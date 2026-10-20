import sys
import os
import json
import logging
import redis
import re
from typing import Any, Dict, List, Optional, Union, Set
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from contextvars import copy_context

# 导入日志模块
from utils.logger import get_logger
from utils.reference_manager import ReferenceManager

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.base_agent import BaseAgent
from models import get_model
from utils.graph import DirectedGraph, NodeExecutionStatus
from workflow.task import TaskWorkflow


class GlobalExecutionAgent(BaseAgent):
    """
    全局执行代理，负责读取planner创建的图，并行执行可执行的节点
    """
    
    def __init__(self, model: str = "deepseek-r1", tools: List[Dict[str, Any]] = None, 
                 config=None, summarizer_model: str = None, **model_kwargs):
        """
        初始化全局执行代理
        
        Args:
            model: 使用的模型名称
            tools: 可用的工具列表
            max_workers: 最大并行工作线程数
            config: GlobalExecutionConfig配置对象
            summarizer_model: 用于总结的模型名称（如果为None则使用model）
            **model_kwargs: 传递给模型的参数
        """
        super().__init__(model=model, tools=tools, config=config)
        self.model_instance = get_model(model, **model_kwargs)
        self.logger = get_logger("GlobalExecutionAgent")
        self._set_logger(self.logger)  # 设置logger到基类
        self.model_kwargs = model_kwargs
        self.config = config or {}
        self.max_workers = self.config.get('max_workers', 10)
        self.summarizer_model = summarizer_model or model  # 如果没有指定summarizer_model，使用默认model
        
        # 执行状态管理
        self.execution_graph: Optional[DirectedGraph] = None
        self.execution_results: Dict[str, Any] = {}
        self.execution_lock = threading.Lock()
        self.is_executing = False
        
        # 创建任务工作流实例池
        self.task_workflows = {}
        
        # 全局参考文献管理器
        self.global_reference_manager = ReferenceManager()
    
    def set_execution_graph(self, graph: DirectedGraph) -> None:
        """
        设置要执行的图
        
        Args:
            graph: planner创建的依赖图
        """
        self.execution_graph = graph
        self.logger.info(f"设置执行图，包含 {len(graph.nodes)} 个节点")
    
    def get_execution_graph(self) -> Optional[DirectedGraph]:
        """
        获取当前执行图
        
        Returns:
            当前执行图
        """
        return self.execution_graph
    
    def execute_graph(self, context: Dict[str, Any] = None, query = None, file_path = None) -> Dict[str, Any]:
        """
        执行当前可执行的一层节点，只执行一次
        
        Args:
            context: 全局上下文信息
            
        Returns:
            执行结果字典
        """
        if not self.execution_graph:
            raise ValueError("没有设置执行图，请先调用 set_execution_graph()")
        
        if self.is_executing:
            raise RuntimeError("图正在执行中，请等待当前执行完成")
        
        self.is_executing = True
        
        try:
            self.logger.info("开始执行当前可执行节点")
            
            # 获取当前可执行节点
            ready_nodes = self.execution_graph.get_ready_nodes()
            self.logger.info(f"当前可执行节点: {ready_nodes}")
            
            # 发送ready_nodes到Redis Stream
            if ready_nodes:
                self.send_redis_event("get_current_execution_nodes", {
                    "node_ids": ready_nodes
                })
            
            if not ready_nodes:
                self.logger.info("没有可执行的节点")
                return {
                    "success": True,
                    "message": "没有可执行的节点",
                    "executed_nodes": [],
                    "results": {}
                }
            
            # 并行执行当前可执行节点
            round_results = self._execute_nodes_parallel(ready_nodes, context or {}, query = query, file_path = file_path)
            
            # 更新执行结果
            with self.execution_lock:
                self.execution_results.update(round_results)
            
            # 标记已执行的节点
            for node_id in ready_nodes:
                self.execution_graph.set_node_status(node_id, NodeExecutionStatus.EXECUTED)
            
            # 获取执行后的图信息
            graph_info = self.execution_graph.get_graph_info()
            
            return {
                "success": True,
                "executed_nodes": ready_nodes,
                "execution_count": len(ready_nodes),
                "results": round_results,
                "graph_info": graph_info,
                "next_ready_nodes": self.execution_graph.get_ready_nodes()
            }
            
        except Exception as e:
            self.logger.error(f"节点执行失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "results": self.execution_results
            }
        finally:
            self.is_executing = False
    
    def _execute_nodes_parallel(self, node_ids: List[str], context: Dict[str, Any], query = None, file_path = None) -> Dict[str, Any]:
        """
        并行执行多个节点
        
        Args:
            node_ids: 要执行的节点ID列表
            context: 上下文信息
            
        Returns:
            执行结果字典 {node_id: result}
        """
        results = {}
        
        # 使用线程池并行执行
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # 提交所有任务
            future_to_node = {
                executor.submit(copy_context().run, self._execute_single_node, node_id, context, query = query, file_path = file_path): node_id
                for node_id in node_ids
            }
            
            # 收集结果
            for future in as_completed(future_to_node):
                node_id = future_to_node[future]
                try:
                    result = future.result()
                    results[node_id] = result
                    self.logger.info(f"节点 {node_id} 执行完成")
                except Exception as e:
                    self.logger.error(f"节点 {node_id} 执行失败: {e}")
                    results[node_id] = {
                        "success": False,
                        "error": str(e)
                    }
        
        return results
    
    def _execute_single_node(self, node_id: str, context: Dict[str, Any], query = None, file_path = None) -> Dict[str, Any]:
        """
        执行单个节点
        
        Args:
            node_id: 节点ID
            context: 上下文信息
            
        Returns:
            节点执行结果
        """
        try:
            # 获取节点信息
            node_attrs = self.execution_graph.get_node_attributes(node_id)
            if not node_attrs:
                raise ValueError(f"节点 {node_id} 不存在")
            
            task = node_attrs.get('task', f'Task {node_id}')
            self.logger.info(f"开始执行节点 {node_id}: {task}")
            
            # 获取或创建任务工作流
            task_workflow = self._get_task_workflow(node_id)
            
            # 构建任务输入
            task_input = {
                "query": query,
                "task": task,
                "knowledge_info": json.dumps(self._get_previous_results(node_id), ensure_ascii=False, indent=2),  # 添加知识信息字段
                "summary_type": node_attrs.get('type', 'search'),
                "file_path": file_path
            }
            
            # 执行任务
            result = task_workflow.execute(task_input)
            
            # 收集该节点的参考文献到全局管理器
            if hasattr(task_workflow, 'execution_agent') and hasattr(task_workflow.execution_agent, 'reference_manager'):
                node_refs = task_workflow.execution_agent.reference_manager.get_reference_list()
                for ref in node_refs:
                    # 传递完整的参考文献信息（URL、标题和类型）
                    self.global_reference_manager.add_url(
                        url=ref['url'],
                        title=ref.get('title'),
                        ref_type=ref.get('type')
                    )
                self.logger.info(f"从节点 {node_id} 收集了 {len(node_refs)} 个参考文献")
                self.logger.info(f"全局参考文献列表: {self.global_reference_manager.get_reference_list()}")
            
            self.logger.info(f"节点 {node_id} 执行完成: {result.get('success', False)}")
            return {
                "node_id": node_id,
                "task": task,
                "success": result.get('success', True),
                "result": result,
                "final_answer": result.get('final_answer', ''),
                "reasoning": result.get('reasoning', ''),
                "subtask_trace": result.get('subtask_trace',''),
            }
            
        except Exception as e:
            self.logger.error(f"执行节点 {node_id} 时发生错误: {e}")
            return {
                "node_id": node_id,
                "success": False,
                "error": str(e),
                "final_answer": f"执行失败: {str(e)}",
            }
    
    def _get_task_workflow(self, node_id: str) -> TaskWorkflow:
        """
        获取或创建任务工作流实例
        
        Args:
            node_id: 节点ID
            
        Returns:
            任务工作流实例
        """
        # 为每个节点创建独立的任务工作流实例，避免状态冲突
        if node_id not in self.task_workflows:
            workflow = TaskWorkflow(
                model=self.model,
                tools=self.tools,
                config=self.config,
                summarizer_model=self.summarizer_model,
                reference_manager=self.global_reference_manager,  # 传递全局参考文献管理器
                **self.model_kwargs
            )
            # 设置task_id到TaskWorkflow
            if self.task_id:
                workflow.set_task_id(self.task_id)
            # 设置node_id到TaskWorkflow
            workflow.set_node_id(node_id)
            self.task_workflows[node_id] = workflow
        
        return self.task_workflows[node_id]
    
    def _get_previous_results(self, node_id: str) -> Dict[str, Any]:
        """
        获取前置节点的执行结果
        
        Args:
            node_id: 当前节点ID
            
        Returns:
            前置节点结果字典
        """
        previous_results = {}
        
        # 获取所有前置节点
        dependent_nodes = self.execution_graph.get_dependent_nodes(node_id)
        
        for dep_node_id in dependent_nodes:
            if dep_node_id in self.execution_results:
                result = self.execution_results[dep_node_id]
                filtered = {
                    k: result.get(k)
                    for k in ("task", "final_answer", "success", "reasoning")
                    if k in result
                }
                
                # 获取当前节点与前置节点的关系
                relationship = self._get_relationship_between_nodes(dep_node_id, node_id)
                if relationship:
                    filtered['relationship'] = relationship
                
                previous_results[dep_node_id] = filtered
        
        return previous_results
    
    def _get_relationship_between_nodes(self, from_node_id: str, to_node_id: str) -> Optional[str]:
        """
        获取两个节点之间边的relationship属性
        
        Args:
            from_node_id: 起始节点ID
            to_node_id: 目标节点ID
            
        Returns:
            Optional[str]: relationship值，如果不存在则返回None
        """
        if not self.execution_graph:
            return None
        
        # 遍历所有边，找到连接这两个节点的边
        for edge_id, edge_data in self.execution_graph.edges.items():
            if (edge_data['from_node'] == from_node_id and 
                edge_data['to_node'] == to_node_id):
                # 返回边的relationship属性
                return edge_data['attributes'].get('relationship')
        
        return None
    
    def get_execution_status(self) -> Dict[str, Any]:
        """
        获取当前执行状态
        
        Returns:
            执行状态信息
        """
        if not self.execution_graph:
            return {"status": "no_graph", "message": "没有设置执行图"}
        
        graph_info = self.execution_graph.get_graph_info()
        
        return {
            "status": "executing" if self.is_executing else "idle",
            "graph_info": graph_info,
            "ready_nodes": self.execution_graph.get_ready_nodes(),
            "execution_results_count": len(self.execution_results),
            "max_workers": self.max_workers
        }
    
    def get_node_result(self, node_id: str) -> Optional[Dict[str, Any]]:
        """
        获取指定节点的执行结果
        
        Args:
            node_id: 节点ID
            
        Returns:
            节点执行结果，如果未执行则返回None
        """
        return self.execution_results.get(node_id)
    
    def reset_execution(self) -> None:
        """
        重置执行状态
        """
        with self.execution_lock:
            self.is_executing = False
            self.execution_results.clear()
            if self.execution_graph:
                self.execution_graph.reset_all_nodes_status()
        
        self.logger.info("执行状态已重置")
    
    def reset_graph_status(self) -> None:
        """
        重置图中所有节点的状态为待执行
        """
        if self.execution_graph:
            self.execution_graph.reset_all_nodes_status()
            self.logger.info("图节点状态已重置")
    
    def clear_execution_results(self) -> None:
        """
        清空执行结果
        """
        with self.execution_lock:
            self.execution_results.clear()
        self.logger.info("执行结果已清空")
    
    def execute(self, input_data: Any = None, query = None, file_path = None) -> Any:
        """
        执行接口，兼容BaseAgent
        
        Args:
            input_data: 输入数据，可以是图对象或包含图的字典
            
        Returns:
            执行结果
        """
        if isinstance(input_data, DirectedGraph):
            self.set_execution_graph(input_data)
            return self.execute_graph()
        elif isinstance(input_data, dict):
            if 'graph' in input_data:
                self.set_execution_graph(input_data['graph'])
                context = input_data.get('context', {})
                return self.execute_graph(context)
            else:
                raise ValueError("输入字典必须包含 'graph' 键")
        else:
            return self.execute_graph(query = query, file_path = file_path)
    
    def visualize_execution_progress(self) -> None:
        """
        可视化当前执行进度
        """
        if not self.execution_graph:
            self.logger.warning("没有设置执行图")
            return
        
        self.logger.info("\n" + "="*80)
        self.logger.info("执行进度可视化")
        self.logger.info("="*80)
        
        # 显示节点状态
        for node_id, node_data in self.execution_graph.nodes.items():
            status = node_data['status']
            attrs = node_data['attributes']
            task = attrs.get('task', f'Task {node_id}')
            
            status_symbol = "✓" if status == NodeExecutionStatus.EXECUTED else "○"
            result_info = ""
            
            if node_id in self.execution_results:
                result = self.execution_results[node_id]
                success = result.get('success', False)
                result_info = f" ({'成功' if success else '失败'})"
            
            self.logger.info(f"[{status_symbol}] {node_id}: {task}{result_info}")
        
        # 显示统计信息
        graph_info = self.execution_graph.get_graph_info()
        self.logger.info(f"\n统计信息:")
        self.logger.info(f"  总节点数: {graph_info['total_nodes']}")
        self.logger.info(f"  已执行: {graph_info['executed_nodes']}")
        self.logger.info(f"  待执行: {graph_info['pending_nodes']}")
        self.logger.info(f"  可执行: {len(self.execution_graph.get_ready_nodes())}")
        self.logger.info("="*80)

    def show_all_nodes_info(self, dict_format = True) -> Dict[str, Any]:
        """
        显示所有节点的详细信息，包括图中的信息和执行结果
        
        Returns:
            包含所有节点信息的字典，格式为 {"nodes": [...], "edges": [...]}
        """
        if not self.execution_graph:
            self.logger.warning("没有设置执行图")
            return {"nodes": [], "edges": []}
        
        # 创建新的图对象来存储增强的节点信息
        result_graph = DirectedGraph()
        
        self.logger.info("="*100)
        self.logger.info("所有节点详细信息")
        self.logger.info("="*100)
        
        # 首先添加所有节点，包含原有属性和新增字段
        for node_id, node_data in self.execution_graph.nodes.items():
            # 获取节点基本信息
            status = node_data['status']
            attrs = node_data['attributes'].copy()  # 复制原有属性
            
            # 添加新的字段
            if node_id in self.execution_results:
                execution_result = self.execution_results[node_id]
                attrs['final_response'] = execution_result.get('final_answer', '')
                attrs['success'] = execution_result.get('success', False)
                attrs['reasoning'] = execution_result.get('reasoning', '')
                # attrs['subtask_trace'] = execution_result.get('subtask_trace', '')
                # attrs['summary'] = execution_result.get('summary', '')
                # attrs['task'] = execution_result.get('task', '')
            else:
                attrs['final_response'] = ''
                attrs['success'] = False
                attrs['reasoning'] = ''
                # attrs['subtask_trace'] = execution_result.get('subtask_trace', '')
                # attrs['summary'] = execution_result.get('summary', '')
                # attrs['task'] = ''
            
            # 添加节点到新图中
            result_graph.add_node(node_id, **attrs)
            # 设置节点状态
            result_graph.set_node_status(node_id, status)
            
            # 打印详细信息
            self.logger.info(f"  节点ID: {node_id}")
            self.logger.info(f"  状态: {status.name if hasattr(status, 'name') else str(status)}")
            self.logger.info(f"  任务: {attrs.get('task', 'N/A')}")
            self.logger.info(f"  类型: {attrs.get('type', 'N/A')}")
            
            # 显示执行结果
            if node_id in self.execution_results:
                execution_result = self.execution_results[node_id]
                success_status = "成功" if execution_result.get('success', False) else "失败"
                self.logger.info(f"  执行状态: {success_status}")
                
                final_answer = execution_result.get('final_answer', '')
                if final_answer:
                    self.logger.info(f"  最终答案: {final_answer}")
                
                reasoning = execution_result.get('reasoning', '')
                if reasoning:
                    self.logger.info(f"  推理过程: {reasoning}")
                
                error = execution_result.get('error', '')
                if error:
                    self.logger.info(f"  错误信息: {error}")
            else:
                self.logger.info(f"  执行状态: 未执行")
            
            self.logger.info("-" * 80)
        
        # 然后复制所有边，保持原有的边结构
        for edge_id, edge_data in self.execution_graph.edges.items():
            from_node = edge_data['from_node']
            to_node = edge_data['to_node']
            edge_attrs = edge_data['attributes']
            
            # 添加边到新图中
            result_graph.add_edge(from_node, to_node, **edge_attrs)

        self.logger.info("="*100)
        
        # 获取拓扑排序结果（按层级显示）
        topological_layers = self._get_topological_layers()
        if topological_layers:
            self.logger.info(f"拓扑排序结果:")
            layer_strs = []
            for layer in topological_layers:
                if len(layer) == 1:
                    layer_strs.append(layer[0])
                else:
                    layer_strs.append(", ".join(sorted(layer)))
            topo_str = " → ".join(layer_strs)
            self.logger.info(f"  {topo_str}")
        else:
            self.logger.info("拓扑排序失败: 图中存在环")
        
        # 显示依赖链（只显示到最终节点的路径）
        self.logger.info(f"依赖链:")
        dependency_chains = self._get_dependency_chains_to_final_nodes()
        for chain in dependency_chains:
            chain_str = " → ".join(chain)
            self.logger.info(f"  {chain_str}")
        
        self.logger.info("="*100)
        
        # 使用 to_dict 方法返回字典格式
        if dict_format:
            return result_graph.to_dict()
        else:
            return result_graph
        

    def get_final_answer(self) -> Optional[str]:
        """
        找到type为answer的节点，并返回它的final_response，并附加全局参考文献
        
        Returns:
            Optional[str]: final_response的值（附加了参考文献），如果未找到answer节点或无final_response则返回None
        """
        for node_id, node_data in self.execution_graph.nodes.items():
            node_attrs = node_data['attributes']
            if node_attrs.get('type') == 'answer':
                final_answer = self.execution_results[node_id].get('final_answer', '')
                
                # 附加全局参考文献
                final_answer_with_refs = self._append_global_references_to_text(final_answer)
                
                return final_answer_with_refs
        
        return None
    
    def _append_global_references_to_text(self, text: str) -> str:
        """
        在文本末尾附加全局参考文献列表
        
        Args:
            text: 原始文本
            
        Returns:
            附加了参考文献的文本
        """
        # 提取文本中使用的引用标签
        used_tags = set(re.findall(r'\[R(\d+)\]', text))
        
        if not used_tags:
            # 如果没有使用引用标签，不添加参考文献
            return text
        
        # 获取所有参考文献
        all_refs = self.global_reference_manager.get_reference_list()
        
        # 只包含被使用的参考文献
        used_refs = []
        for ref in all_refs:
            tag_num = ref['tag'][1:]  # 去掉'R'前缀
            if tag_num in used_tags:
                used_refs.append(ref)
        
        if not used_refs:
            return text
        
        # 构建参考文献部分
        ref_lines = ["\n\n## References"]
        for ref in used_refs:
            ref_lines.append(f"[{ref['tag']}] {ref['url']}")
        
        return text + "\n".join(ref_lines)
    
    def get_global_references(self) -> List[Dict[str, str]]:
        """
        获取全局参考文献列表
        
        Returns:
            包含所有参考文献的列表
        """
        return self.global_reference_manager.get_reference_list()
    
    def clear_global_references(self):
        """清空全局参考文献"""
        self.global_reference_manager.clear()
        self.logger.info("全局参考文献已清空")

    def _get_topological_layers(self) -> Optional[List[List[str]]]:
        """
        获取拓扑排序的层级结构，每一层包含可以并行执行的节点
        
        Returns:
            Optional[List[List[str]]]: 拓扑层级列表，如果存在环则返回None
        """
        if not self.execution_graph or self.execution_graph.has_cycle():
            return None
        
        # 计算每个节点的入度
        in_degree = {node_id: len(self.execution_graph.nodes[node_id]['in_edges']) 
                    for node_id in self.execution_graph.nodes}
        
        layers = []
        remaining_nodes = set(self.execution_graph.nodes.keys())
        
        while remaining_nodes:
            # 找到当前入度为0的所有节点（可以并行执行）
            current_layer = []
            for node_id in list(remaining_nodes):
                if in_degree[node_id] == 0:
                    current_layer.append(node_id)
            
            if not current_layer:
                # 如果没有入度为0的节点，说明有环
                return None
            
            # 添加当前层
            layers.append(sorted(current_layer))
            
            # 移除当前层的节点，并更新后继节点的入度
            for node_id in current_layer:
                remaining_nodes.remove(node_id)
                # 减少所有后继节点的入度
                for edge_id in self.execution_graph.nodes[node_id]['out_edges']:
                    edge = self.execution_graph.edges[edge_id]
                    successor = edge['to_node']
                    in_degree[successor] -= 1
        
        return layers

    def _get_dependency_chains_to_final_nodes(self) -> List[List[str]]:
        """
        获取从源节点到最终节点（叶子节点）的依赖链
        
        Returns:
            List[List[str]]: 到最终节点的依赖链列表
        """
        if not self.execution_graph:
            return []
        
        # 找到所有源节点（没有前置依赖的节点）
        source_nodes = []
        for node_id, node_data in self.execution_graph.nodes.items():
            if len(node_data['in_edges']) == 0:
                source_nodes.append(node_id)
        
        # 找到所有最终节点（没有后继节点的节点）
        final_nodes = []
        for node_id, node_data in self.execution_graph.nodes.items():
            if len(node_data['out_edges']) == 0:
                final_nodes.append(node_id)
        
        all_chains = []
        
        # 从每个源节点到每个最终节点找路径
        for source_node in source_nodes:
            for final_node in final_nodes:
                paths = self._find_paths_between_nodes(source_node, final_node)
                all_chains.extend(paths)
        
        # 去重并排序
        unique_chains = []
        seen_chains = set()
        for chain in all_chains:
            chain_tuple = tuple(chain)
            if chain_tuple not in seen_chains:
                seen_chains.add(chain_tuple)
                unique_chains.append(chain)
        
        # 按链的长度和字典序排序
        unique_chains.sort(key=lambda x: (len(x), x))
        
        return unique_chains
    
    def _find_paths_between_nodes(self, start_node: str, target_node: str) -> List[List[str]]:
        """
        找到从起始节点到目标节点的所有路径
        
        Args:
            start_node: 起始节点ID
            target_node: 目标节点ID
            
        Returns:
            List[List[str]]: 从起始节点到目标节点的所有路径
        """
        if start_node == target_node:
            return [[start_node]]
        
        all_paths = []
        
        def dfs(current_node: str, current_path: List[str], visited: Set[str]):
            # 如果到达目标节点，记录路径
            if current_node == target_node:
                all_paths.append(current_path.copy())
                return
            
            # 获取当前节点的所有后继节点
            for edge_id in self.execution_graph.nodes[current_node]['out_edges']:
                edge = self.execution_graph.edges[edge_id]
                next_node = edge['to_node']
                
                # 避免环路
                if next_node not in visited:
                    new_visited = visited.copy()
                    new_visited.add(current_node)
                    current_path.append(next_node)
                    dfs(next_node, current_path, new_visited)
                    current_path.pop()
        
        # 开始DFS搜索
        dfs(start_node, [start_node], set())
        
        return all_paths
