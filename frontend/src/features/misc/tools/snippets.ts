/**
 * 客户端配置片段（接入页要摊给用户照抄的那几段）。
 *
 * 这一层是**纯函数、不碰 React**：片段里只有两处变量（服务地址、密钥），由调用方
 * 传进来拼好。密钥明文只活在 `AccessPanel` 的组件内存里（`issued`），到这里只是
 * 变成字符串——不落 localStorage、不进 URL、不打日志。
 *
 * 写法照 `docs/外部接入指南-v0.1.md` 里那几段（`[mcp_servers.kybase]` +
 * `bearer_token_env_var`，或 `http_headers = { "Authorization" = "Bearer …" }`），
 * 与《外部接入指南》的口径一致；服务端事实（地址、鉴权头）以 `GET /tools` 给的为准，
 * 所以地址一律插值，**不在源码里写死任何一个地址**。
 *
 * 每段都带一个 `label`：它既是片段上方的标题，也是复制按钮的无障碍名（`复制<label>`）——
 * 一屏上有好几段片段，按钮叫「复制」谁也分不清复制的是哪一段。
 */
export interface Snippet {
  /** 短标签：片段标题 + 复制按钮的无障碍名。 */
  label: string
  /** 这段贴到哪里（配置文件路径 / 客户端界面上的哪一格）。空串 = 不显示这一行。 */
  where: string
  /** 片段正文，整段可复制。 */
  text: string
}

/** 通用表单里的一格：字段名 + 该填的值（值是数据，可整格复制）。 */
export interface FormField {
  label: string
  value: string
}

/** 还没生成密钥时的占位：用户一眼该看出这里要换成自己的那一把。 */
export const TOKEN_PLACEHOLDER = 'kylab_sk_<你的 Key>'

/** 凭据走环境变量时那个变量名（Codex 的 `bearer_token_env_var` 与 `${…}` 展开共用一处）。 */
export const TOKEN_ENV_VAR = 'KYBASE_MCP_KEY'

/** 把凭据写进环境变量的两条命令（两种 shell 各一条，设完都要重开终端）。 */
export function envVarSnippet(token: string): Snippet {
  return {
    label: '写入环境变量',
    where: '',
    text: [
      `setx ${TOKEN_ENV_VAR} "${token}"   # Windows：设完重开终端`,
      `export ${TOKEN_ENV_VAR}="${token}"  # macOS / Linux`,
    ].join('\n'),
  }
}

/** Codex（`~/.codex/config.toml`，CLI / IDE 插件 / 客户端共用这一份）。 */
export function codexSnippets(endpoint: string, token: string): Snippet[] {
  return [
    {
      label: '凭据走环境变量',
      where: '~/.codex/config.toml',
      text: [
        '[mcp_servers.kybase]',
        `url = "${endpoint}"`,
        `bearer_token_env_var = "${TOKEN_ENV_VAR}"`,
      ].join('\n'),
    },
    envVarSnippet(token),
    {
      label: '凭据直接写进配置',
      where: '~/.codex/config.toml',
      text: [
        '[mcp_servers.kybase]',
        `url = "${endpoint}"`,
        `http_headers = { "Authorization" = "Bearer ${token}" }`,
      ].join('\n'),
    },
  ]
}

/**
 * 认 JSON `mcpServers` 的客户端（Cursor 这一路：`~/.cursor/mcp.json`）。
 *
 * **只写 `url` + `headers` 两个键**：远端服务走哪个传输由那个地址自己说（`…/mcp` 就是
 * Streamable HTTP），不额外来一个 `type`——各客户端对 `type` 的取值并不统一，
 * 写错一个键会被整份配置拒掉，而这两个键是它们共同认的。
 */
export function jsonSnippets(endpoint: string, token: string): Snippet[] {
  return [
    {
      label: 'mcp.json 片段',
      where: '~/.cursor/mcp.json',
      text: [
        '{',
        '  "mcpServers": {',
        '    "kybase": {',
        `      "url": "${endpoint}",`,
        '      "headers": {',
        `        "Authorization": "Bearer ${token}"`,
        '      }',
        '    }',
        '  }',
        '}',
      ].join('\n'),
    },
  ]
}

/** 客户端自己会展开 `${VAR}` 时：配置里只留变量名，明文留在环境里。 */
export function envSnippets(endpoint: string, token: string): Snippet[] {
  return [
    {
      label: '从环境变量取 Authorization',
      where: '.mcp.json（支持 ${VAR} 展开的客户端）',
      text: [
        '{',
        '  "mcpServers": {',
        '    "kybase": {',
        '      "type": "http",',
        `      "url": "${endpoint}",`,
        '      "headers": { "Authorization": "Bearer ${' + TOKEN_ENV_VAR + '}" }',
        '    }',
        '  }',
        '}',
      ].join('\n'),
    },
    envVarSnippet(token),
  ]
}

/**
 * 「通用表单」那一档的表头上那句：任何客户端里"新建 MCP 服务器 / 连接器"那个表单
 * （用户截图里那种）都照下面这几格填——**表单式的客户端没有可贴的文件**，
 * 这一档就是为它们准备的。
 */
export const GENERIC_WHERE = '任一客户端的「新建 MCP 服务器 / 连接器」表单都照这几格填'

/**
 * 「通用表单」那一档的逐字段取值：用户在别的客户端里是**一格一格填**的，
 * 所以这里给的是字段与值，而不是一段配置（值同样可整格复制）。
 */
export function genericFields(endpoint: string, token: string): FormField[] {
  return [
    { label: '客户端标识符', value: 'kybase' },
    { label: '显示名称', value: 'KYBASE 知识库' },
    { label: '传输方式', value: 'Streamable HTTP' },
    { label: '服务器 URL', value: endpoint },
    { label: 'Bearer token env var', value: TOKEN_ENV_VAR },
    { label: 'Headers', value: `Authorization: Bearer ${token}` },
  ]
}
