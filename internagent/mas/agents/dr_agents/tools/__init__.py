"""
工具包
"""

import os
import importlib
import sys
import asyncio
from contextvars import copy_context
from types import SimpleNamespace
from typing import Dict, Any, Callable

# 导入日志模块
from utils.logger import get_logger

# 添加当前目录到Python路径（使用insert确保优先级）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 添加camel模块路径（使用insert确保优先从本地camel导入）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class ToolManager:
    """
    工具管理器，负责动态加载和调用工具
    """
    
    def __init__(self, mode: str = "local", config: Dict[str, Any] = None):
        """
        初始化工具管理器
        
        Args:
            mode: 工具模式，'test'使用测试工具，其他值使用本地工具
            config: 配置字典，用于过滤工具
        """
        self.logger = get_logger("ToolManager")
        self.logger.info(f"初始化工具管理器，模式: {mode}")
        self.tools = {}
        self.mode = mode
        self.config = config or {}
        self._load_tools()
    
    def _load_tools(self):
        """加载所有工具"""
        try:
            if self.mode == "test":
                self._load_test_tools()
            else:
                self._load_local_tools()
        except Exception as e:
            self.logger.error(f"加载工具失败: {e}")
    
    def _load_test_tools(self):
        """加载测试工具"""
        # 导入测试工具模块
        import tools.test_tools as tools_module
        
        # 需要排除的类型和模块
        exclude_types = {
            'Any', 'Dict', 'List', 'Optional', 'Union', 'Tuple', 'Set',
            'Callable', 'TypeVar', 'Generic', 'Protocol', 'runtime_checkable'
        }
        
        # 需要排除的函数名
        exclude_functions = {
            'ToolManager', 'get_tool_manager', 're', 'requests', 'datetime'
        }
        
        # 获取模块中所有可调用的函数
        for attr_name in dir(tools_module):
            attr = getattr(tools_module, attr_name)
            # 检查是否是工具函数：可调用、不是私有函数、不是类型注解、有文档字符串、不在排除列表中
            if (callable(attr) and 
                not attr_name.startswith('_') and 
                attr_name not in exclude_types and
                attr_name not in exclude_functions and
                hasattr(attr, '__doc__') and 
                attr.__doc__ and
                attr.__module__ == 'tools.test_tools' and
                not attr_name[0].isupper()):  # 排除类名（首字母大写）
                
                # 创建OpenAI格式的工具配置
                openai_tool = {
                    "name": attr_name,
                    "type": "function",
                    "function_name": attr_name,
                    "description": attr.__doc__,
                    "required_parameters": [],  # 需要从函数签名中提取
                    "parameters": {},  # 需要从函数签名中提取
                    "function": attr  # 保存原始函数
                }
                
                # 尝试从函数签名中提取参数信息
                import inspect
                sig = inspect.signature(attr)
                parameters = {}
                required_params = []
                
                for param_name, param in sig.parameters.items():
                    param_type = "string"  # 默认类型
                    if param.annotation != inspect.Parameter.empty:
                        if param.annotation == str:
                            param_type = "string"
                        elif param.annotation == int:
                            param_type = "integer"
                        elif param.annotation == bool:
                            param_type = "boolean"
                        elif param.annotation == float:
                            param_type = "number"
                    
                    parameters[param_name] = {
                        "type": param_type,
                        "description": f"Parameter: {param_name}"
                    }
                    
                    if param.default == inspect.Parameter.empty:
                        required_params.append(param_name)
                
                openai_tool["parameters"] = parameters
                openai_tool["required_parameters"] = required_params
                
                self.tools[attr_name] = openai_tool
                self.logger.info(f"加载测试工具: {attr_name}")
    
    def _load_local_tools(self):
        """加载本地工具"""
        try:
            # 检查配置中的工具列表，如果为空或None则不加载任何工具
            enabled_tools = self.config.get('tools', {}).get('enabled_tools')
            # enabled_tools 可能是 None（YAML中 enabled_tools: 没有值）或 []（空列表）
            if enabled_tools is None or enabled_tools == []:
                self.logger.info("配置中 tools.enabled_tools 为空，不加载任何工具")
                return
                
            # Resolve the bundled automatic category without changing saved
            # explicit categories or falling back after a selected provider fails.
            from dotenv import load_dotenv
            from tools.parallel_search import (
                resolve_search_provider, search_parallel, fetch_parallel,
            )
            load_dotenv(override=True)
            legacy_tools = [name for name in enabled_tools
                            if name not in ("auto_web_search", "parallel_search")]
            use_parallel = "parallel_search" in enabled_tools
            if "auto_web_search" in enabled_tools:
                provider = resolve_search_provider(self.config)
                if provider == "parallel":
                    use_parallel = True
                else:
                    category = {"volc": "volc_search", "serper": "web_search",
                                "tavily": "tavily_search"}[provider]
                    legacy_tools.append(category)

            local_tools_list = []
            if use_parallel:
                local_tools_list.extend(SimpleNamespace(func=func) for func in
                                        (search_parallel, fetch_parallel))
            if legacy_tools:
                import tool_integration
                legacy_config = {**self.config, "tools": {
                    **self.config.get("tools", {}), "enabled_tools": legacy_tools,
                }}
                local_tools_list.extend(
                    tool_integration.construct_agent_list(config=legacy_config)
                )

            if local_tools_list:
                # our_tools_list = our_tools.construct_our_tools()
                # local_tools_list.extend(our_tools_list)
                
                # 将本地工具转换为OpenAI格式
                for i, tool in enumerate(local_tools_list):
                    # self.logger.debug(f"处理工具 {i}: {type(tool)}")
                    if hasattr(tool, 'func') and callable(tool.func):
                        # 使用工具的函数名作为键
                        tool_name = tool.func.__name__
                        # self.logger.debug(f"找到函数名: {tool_name}")
                        
                        # 获取函数的文档字符串
                        doc = tool.func.__doc__ if hasattr(tool.func, '__doc__') else f"Tool: {tool_name}"
                        
                        # 创建OpenAI格式的工具配置
                        openai_tool = {
                            "name": tool_name,
                            "type": "function",
                            "function_name": tool_name,
                            "description": doc,
                            "required_parameters": [],  # 需要从函数签名中提取
                            "parameters": {},  # 需要从函数签名中提取
                            "local_tool": tool  # 保存原始的本地工具对象
                        }
                        
                        # 尝试从函数签名中提取参数信息
                        import inspect
                        sig = inspect.signature(tool.func)
                        parameters = {}
                        required_params = []
                        
                        for param_name, param in sig.parameters.items():
                            if param_name != 'self':  # 跳过self参数
                                param_type = "string"  # 默认类型
                                if param.annotation != inspect.Parameter.empty:
                                    if param.annotation == str:
                                        param_type = "string"
                                    elif param.annotation == int:
                                        param_type = "integer"
                                    elif param.annotation == bool:
                                        param_type = "boolean"
                                    elif param.annotation == float:
                                        param_type = "number"
                                
                                parameters[param_name] = {
                                    "type": param_type,
                                    "description": f"Parameter: {param_name}"
                                }
                                
                                if param.default == inspect.Parameter.empty:
                                    required_params.append(param_name)
                        
                        openai_tool["parameters"] = parameters
                        openai_tool["required_parameters"] = required_params
                        
                        # self.logger.debug(f"tool_info: {openai_tool}")
                        
                        self.tools[tool_name] = openai_tool
                        self.logger.info(f"加载本地工具: {tool_name}")
                    else:
                        # 如果没有函数名，使用索引
                        tool_name = f"local_tool_{i}"
                        self.logger.warning(f"使用索引名称: {tool_name}")
                        self.tools[tool_name] = tool
                        self.logger.info(f"加载本地工具: {tool_name}")
        except ImportError as e:
            self.logger.error(f"无法导入本地工具模块: {e}")
            self.logger.error("请安装本地工具模块或使用test模式")
        except Exception as e:
            self.logger.error(f"加载本地工具时出错: {e}")
            import traceback
            traceback.print_exc()
    
    def get_tool(self, tool_name: str) -> Callable:
        """
        获取工具函数
        
        Args:
            tool_name: 工具名称
            
        Returns:
            工具函数
        """
        if tool_name not in self.tools:
            raise ValueError(f"工具 '{tool_name}' 不存在")
        
        tool_info = self.tools[tool_name]
        
        # 如果是本地工具（OpenAI格式），返回函数
        if isinstance(tool_info, dict) and "local_tool" in tool_info:
            return tool_info["local_tool"].func
        elif isinstance(tool_info, dict) and "function" in tool_info:
            return tool_info["function"]
        else:
            # 普通工具，直接返回
            return tool_info
    
    def call_tool(self, tool_name: str, **kwargs) -> Any:
        """
        调用工具
        
        Args:
            tool_name: 工具名称
            **kwargs: 工具参数
            
        Returns:
            工具执行结果
        """
        if tool_name not in self.tools:
            raise ValueError(f"工具 '{tool_name}' 不存在")
        
        tool_info = self.tools[tool_name]
        
        # 如果是本地工具（OpenAI格式），需要特殊处理
        if isinstance(tool_info, dict) and "local_tool" in tool_info:
            # 使用本地工具的调用方式
            local_tool = tool_info["local_tool"]
            if hasattr(local_tool, 'run'):
                return local_tool.run(**kwargs)
            else:
                # 检查是否是异步函数
                # 对于绑定方法，需要检查原始函数是否是异步的
                original_func = local_tool.func.__func__ if hasattr(local_tool.func, '__func__') else local_tool.func
                
                # 检查是否是异步函数，包括被装饰器包装的情况
                is_async = asyncio.iscoroutinefunction(original_func)
                
                # 如果直接检查失败，尝试检查被包装的函数
                if not is_async and hasattr(original_func, '__wrapped__'):
                    is_async = asyncio.iscoroutinefunction(original_func.__wrapped__)
                
                # 如果仍然检测不到异步特性，尝试检查源代码
                # 这是因为某些装饰器（如dependencies_required）可能会隐藏异步特性
                if not is_async:
                    import inspect
                    try:
                        source = inspect.getsource(original_func)
                        if "async def" in source:
                            is_async = True
                            self.logger.debug(f"Found 'async def' in source code for {tool_name}, treating as async")
                    except Exception:
                        # 无法获取源代码，保持原始判断结果
                        pass
                
                if is_async:
                    # 如果是异步函数，需要特殊处理
                    import concurrent.futures
                    
                    # 创建一个新的事件循环来运行异步函数
                    def run_async_func():
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        try:
                            coro = local_tool.func(**kwargs)
                            # 协程内部超时，超时会在这里抛出 asyncio.TimeoutError
                            return loop.run_until_complete(asyncio.wait_for(coro, timeout=600))
                        finally:
                            try:
                                loop.stop()
                            except Exception:
                                pass
                            loop.close()

                    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                    try:
                        future = executor.submit(copy_context().run, run_async_func)
                        # 外层不要给 result 再设 timeout；让内部 wait_for 控制
                        return future.result()
                    except asyncio.TimeoutError:
                        # 来自内部 wait_for 的取消/超时，线程能正常结束
                        self.logger.error(f"调用异步工具内部超时: {tool_name}")
                        raise TimeoutError(f"调用工具 {tool_name} 超时")
                    except Exception as e:
                        self.logger.error(f"调用异步工具失败: {tool_name}: {e!r}")
                        raise
                    finally:
                        # 关键：不要等待正在运行的任务
                        executor.shutdown(wait=False)
                else:
                    return local_tool.func(**kwargs)
        elif isinstance(tool_info, dict) and "function" in tool_info:
            # 普通工具（OpenAI格式），直接调用函数
            tool_function = tool_info["function"]
            return tool_function(**kwargs)
        else:
            # 普通工具，直接调用函数
            tool_function = tool_info if callable(tool_info) else tool_info["function"]
            return tool_function(**kwargs)
    
    def list_tools(self) -> list:
        """
        获取所有可用工具列表
        
        Returns:
            工具名称列表
        """
        return list(self.tools.keys())
    
    def get_tool_info(self, tool_name: str) -> Dict[str, Any]:
        """
        获取工具信息
        
        Args:
            tool_name: 工具名称
            
        Returns:
            工具信息字典
        """
        if tool_name not in self.tools:
            return None
        
        tool_info = self.tools[tool_name]
        
        # 如果是本地工具（OpenAI格式），需要特殊处理
        if isinstance(tool_info, dict) and "local_tool" in tool_info:
            return {
                "name": tool_info["name"],
                "function": tool_info["local_tool"].func,
                "doc": tool_info["description"],
                "module": tool_info["local_tool"].func.__module__,
                "parameters": tool_info["parameters"],
                "required_parameters": tool_info["required_parameters"]
            }
        elif isinstance(tool_info, dict) and "function" in tool_info:
            return {
                "name": tool_info["name"],
                "function": tool_info["function"],
                "doc": tool_info["description"],
                "module": tool_info["function"].__module__,
                "parameters": tool_info["parameters"],
                "required_parameters": tool_info["required_parameters"]
            }
        else:
            # 普通工具
            tool_function = tool_info if callable(tool_info) else tool_info["function"]
            return {
                "name": tool_name,
                "function": tool_function,
                "doc": tool_function.__doc__,
                "module": tool_function.__module__,
            }
        
    def get_simple_tool_info(self, tool_name: str) -> Dict[str, Any]:
        """
        获取工具信息
        
        Args:
            tool_name: 工具名称
            
        Returns:
            工具信息字典
        """
        tool_info = self.get_tool_info(tool_name)
        return {
            "function_name": tool_info["name"],
            "description": tool_info["doc"],
        }


# 创建全局工具管理器实例（默认使用本地工具）
tool_manager = None


def get_tool_manager(mode: str = "local", config: Dict[str, Any] = None) -> ToolManager:
    """
    获取工具管理器实例
    
    Args:
        mode: 工具模式，'test'使用测试工具，其他值使用本地工具
        config: 配置字典，用于过滤工具
        
    Returns:
        工具管理器实例
    """
    global tool_manager
    config = config or {}

    if tool_manager is None:
        tool_manager = ToolManager(mode, config)
    else:
    
        # 比较配置内容而不是对象引用
        current_config = tool_manager.config
        # 获取当前和新的 enabled_tools，如果是 None 则转为 [] 进行比较
        current_tools = current_config.get('tools', {}).get('enabled_tools')
        new_tools = config.get('tools', {}).get('enabled_tools')
        # None 和 [] 都视为空配置
        current_tools = current_tools if current_tools is not None else []
        new_tools = new_tools if new_tools is not None else []
        
        config_changed = (
            tool_manager.mode != mode or 
            current_tools != new_tools or
            current_config.get('tools', {}).get('web_search_provider', 'auto') !=
            config.get('tools', {}).get('web_search_provider', 'auto')
        )
    
        # 如果模式或配置改变，重新创建工具管理器
        if config_changed:
            tool_manager = ToolManager(mode, config)
    
    return tool_manager 