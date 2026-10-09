"""MCP Server（stdio / Streamable HTTP，M4 实现）。

纪律（工程规范 §3.3）：与 ``api/`` 一样只做协议适配，工具实现一律转发 ``services/``，
保证 REST 与 MCP 两种接入能力等价（架构设计 v0.2 §3.1）。
"""
