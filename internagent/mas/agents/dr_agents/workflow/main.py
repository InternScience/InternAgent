#!/usr/bin/env python3
"""
GlobalExecutionAgent 使用示例

这个脚本演示了如何使用 GlobalExecutionAgent 来执行 planner 创建的图
"""

import sys
import os
import json
import redis
import time
import argparse
from datetime import datetime
# 添加路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.base_agent import BaseAgent
from agents.global_execution_agent import GlobalExecutionAgent
from agents.global_planner_agent import GlobalPlannerAgent
from agents.coordinator_agent import CoordinatorAgent
from agents.synthesizer_agent import SynthesizerAgent
from utils.graph import DirectedGraph, NodeExecutionStatus

from utils.search_context import search_run
from utils.logger import get_logger
logger = get_logger("main")


# 导入配置加载器
from utils.config_loader import load_config, get_config

# 导入模型
from models import get_model



class Workflow(BaseAgent):
    def __init__(self, config=None):
        """
        初始化Workflow
        
        Args:
            config: 配置字典，如果为None则使用默认配置
        """
        # 获取或使用传入的配置
        self.config = config if config is not None else get_config()
        
        # 调用基类初始化
        super().__init__(config=self.config)
        self._set_logger(logger)  # 设置logger到基类
        
        # 获取模型配置
        model_config = self.config.get('model', {})
        default_model = model_config.get('default_model', 'o4-mini')
        
        # 处理 global_execution_model 配置（可能是字典或字符串）
        execution_model_config = model_config.get('global_execution_model')
        if isinstance(execution_model_config, dict):
            # 新的细分配置
            execution_model = execution_model_config.get('execution_model') or default_model
            summarizer_model = execution_model_config.get('summarizer_model') or default_model
        else:
            # 旧的简单配置（向后兼容）
            execution_model = execution_model_config or default_model
            summarizer_model = None
        
        # 使用配置初始化各个Agent，只传递各自需要的配置部分
        self.global_planner = GlobalPlannerAgent(
            model=model_config.get('global_planner_model') or default_model,
            config=self.config.get('global_planner', {})
        )
        self.global_execution_agent = GlobalExecutionAgent(
            model=execution_model,
            summarizer_model=summarizer_model,
            config=self.config.get('global_execution', {})
        )
        self.coordinator_agent = CoordinatorAgent(
            model=model_config.get('coordinator_model') or default_model,
            config=self.config.get('coordinator', {})
        )
        self.synthesizer_agent = SynthesizerAgent(
            model=model_config.get('synthesizer_model') or default_model,
            config=self.config.get('synthesizer', {})
        )
        
        # 初始化用于查询分析的模型实例
        self.analysis_model = get_model('o4-mini')
    
    def _save_synthesizer_input(self, task: str, synthesizer_input: dict, task_id: str = None, timestamp: str = None):
        """
        保存synthesizer_input到本地文件
        Args:
            task: 任务描述
            synthesizer_input: 要保存的synthesizer输入数据
            task_id: 任务ID，用于生成文件名
            timestamp: 时间戳，如果提供则使用，否则生成新的
        """
        try:
            # 创建保存目录
            save_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "synthesizer_input")
            os.makedirs(save_dir, exist_ok=True)
            
            # 生成文件名
            if timestamp is None:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            if task_id:
                filename = f"{task[:10]}_{task_id}.json"
            else:
                filename = f"{task[:10]}_{timestamp}.json"
            
            filepath = os.path.join(save_dir, filename)
            
            # 准备可序列化的数据
            serializable_input = {
                "question": synthesizer_input["question"],
                "graph": synthesizer_input["graph"].to_dict() if hasattr(synthesizer_input["graph"], "to_dict") else synthesizer_input["graph"],
                "node_id": synthesizer_input["node_id"],
                # reference_manager不能直接序列化，保存其引用列表
                "references": synthesizer_input["reference_manager"].get_reference_list() if synthesizer_input.get("reference_manager") else []
            }
            
            # 保存到文件
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(serializable_input, f, ensure_ascii=False, indent=2)
            
            logger.info(f"Synthesizer input saved to: {filepath}")
            
        except Exception as e:
            logger.error(f"Failed to save synthesizer input: {e}")

    @search_run()
    def execute(self, task, file_path = None, task_id = None, save_synthesizer_input = False, timestamp = None):
        """
        执行工作流
        
        Args:
            task: 任务描述
            file_path: 附加文件路径
            task_id: 任务ID，用于发送redis事件
            save_synthesizer_input: 是否保存synthesizer输入
            timestamp: 时间戳，用于保持文件命名一致性
        
        Returns:
            最终答案
        """
        # 保存timestamp供后续使用
        self.timestamp = timestamp if timestamp else datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # 从配置中获取参数
        max_execution_layers = self.config.get('main', {}).get('max_iter', 10)
        
        # 设置task_id到所有agent（如果提供了）
        if task_id:
            self.set_task_id(task_id)
            self.global_planner.set_task_id(task_id)
            self.global_execution_agent.set_task_id(task_id)
            self.coordinator_agent.set_task_id(task_id)
            self.synthesizer_agent.set_task_id(task_id)
        
        self.global_planner.execute(task, file_path=file_path)

        cur_graph = self.global_planner.graph.to_dict()
        logger.info(f"Planner_Graph: {cur_graph}")

        graph = self.global_planner.graph
        cnt = 0

        while True:
            cnt += 1
            self.global_execution_agent.set_execution_graph(graph)
            self.global_execution_agent.show_all_nodes_info(dict_format=False)

            logger.info(f"执行第{cnt}层...")

            result = self.global_execution_agent.execute(query = task, file_path = file_path)
            logger.info(f"第{cnt}层执行结果:")
            logger.info(json.dumps(result, ensure_ascii=False, indent=2))
            current_graph = self.global_execution_agent.show_all_nodes_info(dict_format=False)

            logger.info(f"After_Execution_Graph: {current_graph.to_dict()}")

            if current_graph.get_ready_nodes() == []:
                logger.error("No ready nodes, workflow finished")
                break
            
            # 防止层数过多，导致无法运行到answer节点
            if cnt > max_execution_layers:
                answer_node_id = None
                for node_id, node_data in current_graph.nodes.items():
                    node_attrs = node_data['attributes']
                    if node_attrs.get('type') == 'answer':
                        answer_node_id = node_id
                        break
                if answer_node_id is not None:
                    synthesizer_input = {
                        "question": task,
                        "graph": current_graph,
                        "node_id": answer_node_id,
                        "reference_manager": self.global_execution_agent.global_reference_manager,
                        "timestamp": self.timestamp
                    }
                    
                    # 保存synthesizer_input到本地文件
                    if save_synthesizer_input:
                        self._save_synthesizer_input(task, synthesizer_input, task_id, self.timestamp)
                    
                    redis_event = {}
                    self.send_redis_event("start_answer_node", redis_event)
                    final_answer = self.synthesizer_agent.execute(synthesizer_input)
                    logger.info(f"Final Answer for {task}: {final_answer}")
                    return final_answer

            if self.config.get('main', {}).get('enable_coordinator', False):

                coordinator_input = {
                    "graph": current_graph,
                    "query": task
                }
                graph = self.coordinator_agent.execute(coordinator_input)
                logger.info(f"Coordinator_Graph: {graph.to_dict()}")

            else:
                graph = current_graph

            # 检查ready_nodes中是否有answer类型的节点
            answer_node_id = None
            for node_id in graph.get_ready_nodes():
                if graph.get_node_attributes(node_id)['type'] == 'answer':
                    answer_node_id = node_id
                    break
            
            if answer_node_id is not None:
                synthesizer_input = {
                    "question": task,
                    "graph": graph,
                    "node_id": answer_node_id,
                    "reference_manager": self.global_execution_agent.global_reference_manager
                }
                
                # 保存synthesizer_input到本地文件
                if save_synthesizer_input:
                    self._save_synthesizer_input(task, synthesizer_input, task_id, self.timestamp)

                redis_event = {}
                self.send_redis_event("start_answer_node", redis_event)
                final_answer = self.synthesizer_agent.execute(synthesizer_input)
                logger.info(f"Final Answer for {task}: {final_answer}")
                return final_answer

        final_answer = self.global_execution_agent.get_final_answer()
        logger.error(f"Wrong workflow, Final Answer: {final_answer}")
        return final_answer

def main():
    """
    主函数
    支持命令行参数：
    --config: 配置文件路径
    --query: 研究任务/查询
    --save_path: 结果保存路径
    --file: 附加文件路径（可选）
    """

    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # 解析命令行参数
    parser = argparse.ArgumentParser(description='InternResearch Workflow')
    parser.add_argument('--config', type=str, default=os.path.join(BASE_DIR, "configs", "config_report.yaml"),
                        help='配置文件路径 (YAML格式)')
    parser.add_argument('--query', type=str, default="""Task Background
Combination therapy is a fundamental strategy in oncology, which may help overcome drug resistance and enhance therapeutic efficacy through synergistic mechanisms. Given a specific tumor cell line and a panel of candidate monotherapy drugs, the objective is to identify two-drug combinations with potential synergistic anti-tumor activity.
Test Case 3: Lung Cancer Cell Line A549
Cell Line: A549 (Human non-small cell lung cancer)
Candidate Drugs:
Belinostat, Niraparib, Bleomycin sulfate, Mitoxantrone 2HCl, Afatinib, Venetoclax, Pipobroman, Carfilzomib, Binimetinib, Entinostat, Leucovorin Calcium Pentahydrate, ORY-1001, Vismodegib, Neratinib, Ixazomib, Nilotinib hydrochloride, Palbociclib, Panobinostat, Daunorubicin HCl, Vorinostat, Sclareol, Oxaliplatin, (S)-(−)-Limonene, Asiatic Acid, Madecassic acid
Output: List recommended two-drug combinations (format: Drug A + Drug B)
""",
                        help='研究任务/查询')
    parser.add_argument('--save_path', type=str, default=None,
                        help='结果保存路径（可选，如果不提供则自动生成）')
    parser.add_argument('--file', type=str, default=None,
                        help='附加文件路径（可选）')
    parser.add_argument('--save_synthesizer_input', type=bool, default=True,
                        help='是否保存synthesizer_input到本地文件')
    
    args = parser.parse_args()

    save_synthesizer_input = args.save_synthesizer_input
    
    try:    
        # 加载配置
        config = load_config(args.config)
        logger.info(f"从配置文件加载配置: {args.config}")

        task = args.query
        
        # 生成统一的timestamp，用于所有保存操作
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        print("task: ", task)
        
        # 执行工作流
        workflow = Workflow(config=config)
        result = workflow.execute(task, file_path=args.file, save_synthesizer_input=save_synthesizer_input, timestamp=timestamp)
        logger.info(f"Result: {result}")
        
        # 确定保存路径
        if args.save_path:
            # 如果传入了save_path，就使用传入的路径
            output_file = args.save_path
            # 确保输出目录存在
            output_dir = os.path.dirname(output_file)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
        else:
            # 否则自动生成路径 - 使用query前10个字符和timestamp生成文件名
            filename = f"{task[:10]}_{timestamp}.md"
            test_results_dir = os.path.join(BASE_DIR, "test_results")
            os.makedirs(test_results_dir, exist_ok=True)
            output_file = os.path.join(test_results_dir, filename)
        
        # 保存结果
        with open(output_file, "w", encoding="utf-8") as f:
            # 如果 result 是 dict，可以用 json.dumps 美化输出
            if isinstance(result, dict):
                f.write("```json\n")
                f.write(json.dumps(result, indent=4, ensure_ascii=False))
                f.write("\n```")
            else:
                f.write(str(result))

        print(f"结果被保存至 {output_file}")
        print("\n" + "="*60)
        print("演示完成！")
        print("="*60)
    
    except Exception as e:
        print(f"演示过程中发生错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
