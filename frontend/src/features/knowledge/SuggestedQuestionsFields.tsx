/**
 * 「为每个分段生成推荐问题」的四个设置项（v23；旧 `SuggestedQuestionsFields.vue` 同一份）。
 *
 * 建库弹窗与知识库设置**共用同一份控件**：同一组设置出现两套控件，迟早会出现
 * "建库时能关、设置里关不掉"这种说不清的差别。四个值都由调用方持有
 * （建库时是提交用的草稿，设置里是脏检查用的草稿）。
 *
 * 它跟的是**分段**，不是"空状态的展示"：入库时为每一段让模型出几个问题，
 * 问题并进该段的检索文本，于是用户换个问法也能命中同一段。
 */
import { useEffect } from 'react'

import {
  SUGGESTED_COUNT_DEFAULT,
  SUGGESTED_COUNT_MAX,
  SUGGESTED_COUNT_MIN,
  SUGGESTED_PROMPT_MAX_CHARS,
} from '@/api/knowledgeBases'
import { InfoTip } from '@/features/knowledge/composites'
import { RangeField } from '@/features/knowledge/RangeField'
import { useModelRegistry } from '@/features/knowledge/store'
import { Checkbox } from '@/ui/checkbox'
import { Label } from '@/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/select'
import { Textarea } from '@/ui/textarea'

export interface SuggestedQuestionsValue {
  enabled: boolean
  count: number
  modelPk: string
  prompt: string
}

interface SuggestedQuestionsFieldsProps {
  value: SuggestedQuestionsValue
  onChange: (patch: Partial<SuggestedQuestionsValue>) => void
}

/** 轨道上的常用值：1 条太薄、2 条兜底、3 条是默认、5 条是上限。 */
const COUNT_MARKS = [
  { value: 2 },
  { value: SUGGESTED_COUNT_DEFAULT, primary: true },
  { value: SUGGESTED_COUNT_MAX },
]

/**
 * 「跟随对话模型」那一项的取值。
 *
 * Radix 的 `<Select.Item>` **不接受空串**（空串被它留作"没有选择"），而这里的值域里
 * `''` 正是"跟随对话模型"。所以界面上用一个哨兵值，进出都换回 `''`——
 * 对外的契约（`SuggestedQuestionsValue.modelPk`）一点没变。
 */
const FOLLOW_CHAT_MODEL = '__follow__'

export function SuggestedQuestionsFields({ value, onChange }: SuggestedQuestionsFieldsProps) {
  const registry = useModelRegistry()

  useEffect(() => {
    // 注册表是缓存 + 后台刷新：拿不到就只显示"跟随对话模型"那一项，
    // 不把"没加载出来"演成一个错误
    void registry.prefetch()
    // 只在挂载时预取一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const modelOptions = [
    { value: FOLLOW_CHAT_MODEL, label: '跟随对话模型' },
    ...registry.chatModels.map((model) => ({
      value: model.id,
      label: model.label || model.model_id,
    })),
  ]

  return (
    <div className="kb-suggested">
      <div>
        {/* 14px / 400：这一行是"读得到的选项文字"，label 原语的 12px / 500 是字段标签口径
            （`.kb-switch` 只留布局，字号必须写在调用点——原语内部走 `cn()`，后写者为准） */}
        <Label className="kb-switch text-[length:var(--text-meta-size)] font-normal">
          <Checkbox
            checked={value.enabled}
            aria-label="为每个切块生成推荐问题"
            onCheckedChange={(checked) => onChange({ enabled: checked === true })}
          />
          <span>为每个切块生成推荐问题</span>
        </Label>
      </div>

      <div className="field">
        <span className="field-label">每个切块生成几条</span>
        <RangeField
          value={value.count}
          onChange={(count) => onChange({ count })}
          min={SUGGESTED_COUNT_MIN}
          max={SUGGESTED_COUNT_MAX}
          marks={COUNT_MARKS}
          ariaLabel="每个切块生成几条问题"
        />
      </div>

      <div className="field">
        <span className="field-label">
          出题用的模型
          <InfoTip text="默认跟随对话页当前选的模型。" />
        </span>
        <Select
          value={value.modelPk || FOLLOW_CHAT_MODEL}
          onValueChange={(modelPk) =>
            onChange({ modelPk: modelPk === FOLLOW_CHAT_MODEL ? '' : modelPk })
          }
        >
          <SelectTrigger aria-label="出题用的模型">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {modelOptions.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="field">
        <span className="field-label">
          自定义出题提示词 <span className="field-optional">可选</span>
        </span>
        <Textarea
          value={value.prompt}
          rows={4}
          maxLength={SUGGESTED_PROMPT_MAX_CHARS}
          placeholder="留空使用内置提示词"
          aria-label="自定义出题提示词"
          onChange={(event) => onChange({ prompt: event.target.value })}
        />
        {/* 只说这一格怎么写：留空是内置；`{n}` 是条数；要求每行一个问题（解析靠它） */}
        <p className="text-hint">
          留空即用内置提示词。用 <code>{'{n}'}</code> 表示条数，并要求模型
          <strong>每行输出一个问题</strong>。
        </p>
      </div>
    </div>
  )
}
