/**
 * 自定义 ESLint 规则：kylab/no-emoji
 *
 * 依据《前端设计规范 v0.14》§3（图标系统）与 §8（禁止项）、
 * 《项目工程规范 v0.5》§4.3（风格纪律）：
 * **禁止 emoji 出现在任何源码字符串中**；图标只用依赖里的两套图标库
 * （`lucide-react` 为主、`@remixicon/react` 用于侧栏导航与品牌位）与
 * `features/layout/icons.tsx` 里那几个手绘内联 SVG。
 *
 * 检查范围：字符串字面量、模板字符串、JSX 文本。
 * 不检查注释——禁令针对 UI 呈现，注释不进入界面。
 */

// 与 scripts/scan_emoji.py 的区段保持一致（两个扫描器的范围必须同一套）。
// 变体选择符 FE00-FE0F 段是刻意保留的：键帽类 emoji（如数字加 FE0F 组合）只有靠它才拦得住，
// 但核心规则 no-misleading-character-class 会把它误判为多码位字符类，故就地屏蔽。
const EMOJI_RE =
  // eslint-disable-next-line no-misleading-character-class
  /[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{1F1E6}-\u{1F1FF}\u{2B00}-\u{2BFF}\u{FE00}-\u{FE0F}\u{2190}-\u{21FF}]/u

// 文本排版合法字符白名单（引号、破折号、省略号、箭头、数学符号等），不误报
const WHITELIST = new Set([
  '\u201C',
  '\u201D',
  '\u2018',
  '\u2019',
  '\u2016', // ‖ 范数符号：技术文档里合法，与 emoji 无关
  '\u2014',
  '\u2013',
  '\u2026',
  '\u00B7',
  '\u2022',
  '\u2039',
  '\u203A',
  '\u00AB',
  '\u00BB',
  '\u2190',
  '\u2192',
  '\u2191',
  '\u2193',
  '\u2194',
])

/** @type {import('eslint').Rule.RuleModule} */
const noEmojiRule = {
  meta: {
    type: 'problem',
    docs: {
      description: '禁止 emoji 出现在源码字符串中（UI 全链路禁 emoji）',
      url: 'https://gitee.com/kkomia/kylab',
    },
    schema: [],
    messages: {
      noEmoji:
        '禁止使用 emoji（{{char}}，U+{{code}}）。图标请用 lucide-react / @remixicon/react，见《项目工程规范 v0.5》§4.3。',
    },
  },

  create(context) {
    function check(node, value) {
      if (typeof value !== 'string') return
      for (const char of value) {
        if (!EMOJI_RE.test(char) || WHITELIST.has(char)) continue
        context.report({
          node,
          messageId: 'noEmoji',
          data: {
            char,
            code: char.codePointAt(0).toString(16).toUpperCase().padStart(4, '0'),
          },
        })
      }
    }

    return {
      Literal(node) {
        check(node, node.value)
      },
      TemplateElement(node) {
        check(node, node.value.raw)
      },
      // JSX 里直接写的文字（`<span>🎉 完成</span>`）。
      // **这条以前没有**：规则只认字面量与模板串，于是"直接写在标签里的 emoji"
      // 只能靠 `scripts/scan_emoji.py` 那层兜底——编辑器里当时是看不见的。
      JSXText(node) {
        check(node, node.value)
      },
      // Vue 模板里的纯文本节点（由 vue-eslint-parser 提供）。
      // 本仓前端已无 `.vue`，留着是为了规则本身仍可复用到别处。
      VText(node) {
        check(node, node.value)
      },
    }
  },
}

export default noEmojiRule
