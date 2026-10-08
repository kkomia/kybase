/**
 * 知识库域（C 域）的对外出口。
 *
 * 主控接线时按这份清单挂路由（`src/app/**` 归主控，本域不改它）：
 *
 * ```tsx
 * <Route path="/knowledge-bases" element={<KnowledgeBasesView />} />
 * <Route path="/kb/:kbId" element={<KnowledgeBaseView />} />
 * <Route path="/kb/:kbId/wiki" element={<WikiView />} />
 * <Route path="/documents/:documentId" element={<DocumentView />} />
 * ```
 *
 * 三个页面都**可选地**接受 id prop：不传就从路由参数取（`useParams`），
 * 所以在 Router 里直接挂即可；测试里也可以直接传 prop。
 *
 * 依赖（见最终报告的"需要主控做的事"）：
 * - Toast 容器（sonner 的 `<Toaster/>`）由应用壳挂载，本域只调 `toast.*`；
 * - 文档预览已走 `@/features/preview` 的 `<FilePreview/>`（已就绪）；
 * - 界面原语一律用 `src/ui/**`（shadcn 原语）；本域自写的 `primitives.tsx` 已删除，
 *   只留下 `composites.tsx` 里那几个"由 `@/ui/*` 拼出来"的组合件（状态标签/骨架预设/空态/
 *   分段进度条/说明气泡），各自"为什么不是换掉"写在那个文件的表里。
 *
 * 换原语时的对照（旧 `primitives.tsx` → 现在）：
 *
 * | 旧 | 现在 |
 * |----|------|
 * | `Button variant="primary"` | `@/ui/button`（**默认档**就是墨色实心） |
 * | `Button`（无 variant，描边中性） | `@/ui/button variant="outline"` |
 * | `Button variant="subtle"` | `variant="secondary"`（`--bg-subtle` 底） |
 * | `Button variant="ghost"` / `IconButton` | `variant="ghost"`（图标按钮加 `size="icon"` / `"icon-sm"`） |
 * | `Button variant="danger"` | `variant="destructive"`（红字，不是红实心） |
 * | `Input` / `Textarea` | `@/ui/input` / `@/ui/textarea`（字号 14 → 15px，见 ui/README §2.2） |
 * | `Select`（原生） | `@/ui/select`（Radix；`''` 作值的地方用了哨兵值，见各页面注释） |
 * | `Modal` | `@/ui/dialog`（弹窗宽 560 → 480、内边距 20 → 16，见 ui/README §2.2） |
 * | `ConfirmDialog` | `@/ui/alert-dialog`（role 变 `alertdialog`，点确认即关闭、Esc 不再关） |
 * | `RowMenu` / `MenuItem` | `@/ui/dropdown-menu`（走 Portal，选中即自动关闭） |
 * | `StatusTag` / `Skeleton` / `EmptyState` / `MeterBar` / `InfoTip` | `./composites.tsx` 里的同名（或 `SkeletonRows`）组合件 |
 * | 原生 `<input type=checkbox>` / `<input type=radio>` | `@/ui/checkbox`（半选用 `'indeterminate'`）/ `@/ui/radio-group` |
 *
 * **本域不再对外转出任何 UI 组件**（`Button` / `Modal` / `Select` 这类）：
 * 需要的话直接用 `@/ui/button` 等。旧引用（若有）请一并改成 `@/ui/*`。
 */
export { KnowledgeBasesView } from './KnowledgeBasesView'
export { KnowledgeBaseView } from './KnowledgeBaseView'
export { WikiView } from './WikiView'
export { DocumentView } from './DocumentView'
export { ProviderRoute, providerBlockedMessage } from './ProviderRoute'
export { DocumentDrawer } from './DocumentDrawer'
export { ProcessingTimeline } from './ProcessingTimeline'
export { ShareDialog } from './ShareDialog'
export { UploadDialog } from './UploadDialog'
export { SourcePanel } from './SourcePanel'
export { KbSearchPanel } from './KbSearchPanel'
export { KnowledgeBaseSettings } from './KnowledgeBaseSettings'
export { SuggestedQuestionsFields, type SuggestedQuestionsValue } from './SuggestedQuestionsFields'
export { RangeField, type RangeMark } from './RangeField'
export {
  EmptyState,
  InfoTip,
  MeterBar,
  SkeletonRows,
  StatusTag,
  type MeterSegment,
  type MeterTone,
  type StatusTone,
} from './composites'
export { Markdown, decorate, type Citation } from './markdown'
export { documentStageView, FILTER_STAGE_KEYS, type StatusView } from './status'
export { chunkingErrorOf, numberOr, parseIntOrNull } from './chunking'
export {
  MAX_UPLOAD_BYTES,
  MAX_UPLOAD_FILES,
  MAX_UPLOAD_MB,
  UPLOAD_FORMAT_HINT,
  formatHintOf,
  limitsOf,
  useUploadLimits,
  type UploadLimits,
} from './uploadLimits'
export {
  messageOf,
  notify,
  resetKnowledgeBaseCache,
  resetRegistryCache,
  useKnowledgeBases,
  useModelRegistry,
  usePolling,
  type KnowledgeBaseStore,
  type RegistryStore,
} from './store'
