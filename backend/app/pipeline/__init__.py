"""摄入流水线状态机（M2 实现）。

状态序列见架构设计 v0.2 §4：
``uploaded → probing → parsing → parsed → chunking → chunked → embedding → indexed``，
可选增强分支 ``enriching → enriched`` 默认关闭且失败不影响主链路。
"""
