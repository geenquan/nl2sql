class QueryAgent:
    """查询型智能体编排入口；首版应是受控工作流，不是任意自主执行代理。"""

    def answer(self, question: str, user_context: dict) -> dict:
        # TODO(M7): 依次调用语义检索、NL2SQL、安全检查、只读执行和结果解释。
        # TODO(M7): 限制最大步骤、重试、超时和成本；每步都可审计。
        raise NotImplementedError("TODO(M7): query agent")
