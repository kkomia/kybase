"""摄入流水线状态机（ 实现）。

状态序列见本项目 4：
``uploaded → probing → parsing → parsed → chunking → chunked → embedding → indexed``，
可选增强分支 ``enriching → enriched`` 默认关闭且失败不影响主链路。
"""
