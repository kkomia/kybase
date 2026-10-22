"""MCP Server（stdio / Streamable HTTP， 实现）。

纪律（分层规则 3.3）：与 ``api/`` 一样只做协议适配，工具实现一律转发 ``services/``，
保证 REST 与 MCP 两种接入能力等价（本项目 3.1）。
"""
