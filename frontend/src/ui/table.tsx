// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * 数据表。表头照 tokens.css 的 `.panel-head` 给一层 `--bg-subtle` 底
 * （"表头是面板内部的一层底色，它把'这是表头、下面是数据'说清楚了"），
 * 行悬停用 alpha 填充 `--bg-hover`、选中用 `--bg-selected`。
 *
 * 行高不写死：数据表的行要能跟着单元格内容长；列表行的 44px 口径
 * （`--row-height`）由调用方自己加 `h-[var(--row-height)]`。
 */
function Table({ className, ...props }: React.ComponentProps<'table'>) {
  return (
    <div data-slot="table-container" className="relative w-full overflow-x-auto">
      <table
        data-slot="table"
        className={cn('w-full caption-bottom text-[length:var(--text-meta-size)]', className)}
        {...props}
      />
    </div>
  )
}

function TableHeader({ className, ...props }: React.ComponentProps<'thead'>) {
  return (
    <thead
      data-slot="table-header"
      className={cn('[&_tr]:border-b [&_tr]:border-[var(--border-hairline)]', className)}
      {...props}
    />
  )
}

function TableBody({ className, ...props }: React.ComponentProps<'tbody'>) {
  return (
    <tbody
      data-slot="table-body"
      className={cn('[&_tr:last-child]:border-0', className)}
      {...props}
    />
  )
}

function TableFooter({ className, ...props }: React.ComponentProps<'tfoot'>) {
  return (
    <tfoot
      data-slot="table-footer"
      className={cn(
        'border-t border-[var(--border-hairline)] bg-[var(--bg-subtle)] font-medium [&>tr]:last:border-b-0',
        className,
      )}
      {...props}
    />
  )
}

function TableRow({ className, ...props }: React.ComponentProps<'tr'>) {
  return (
    <tr
      data-slot="table-row"
      className={cn(
        'border-b border-[var(--border-hairline)] transition-colors hover:bg-[var(--bg-hover)] has-aria-expanded:bg-[var(--bg-hover)] data-[state=selected]:bg-[var(--bg-selected)]',
        className,
      )}
      {...props}
    />
  )
}

function TableHead({ className, ...props }: React.ComponentProps<'th'>) {
  return (
    <th
      data-slot="table-head"
      className={cn(
        'h-9 bg-[var(--bg-subtle)] px-3 text-left align-middle text-[length:var(--text-micro-size)] font-medium whitespace-nowrap text-text-secondary [&:has([role=checkbox])]:pr-0',
        className,
      )}
      {...props}
    />
  )
}

function TableCell({ className, ...props }: React.ComponentProps<'td'>) {
  return (
    <td
      data-slot="table-cell"
      className={cn('px-3 py-2 align-middle [&:has([role=checkbox])]:pr-0', className)}
      {...props}
    />
  )
}

function TableCaption({ className, ...props }: React.ComponentProps<'caption'>) {
  return (
    <caption
      data-slot="table-caption"
      className={cn('mt-4 text-[length:var(--text-meta-size)] text-text-tertiary', className)}
      {...props}
    />
  )
}

export { Table, TableHeader, TableBody, TableFooter, TableHead, TableRow, TableCell, TableCaption }
