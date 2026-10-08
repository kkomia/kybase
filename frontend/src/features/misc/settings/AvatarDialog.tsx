/**
 * 换头像（v0.29）——与旧前端 `components/settings/AvatarDialog.vue` 对应。
 *
 * 两处值得说清楚：
 * 1. **前端先缩到 256px**（canvas）。服务端不装成像库，它只守"是不是图、有多大"；
 *    而"一张 4MB 的手机照片"是用户默认会选的东西——不缩的话每次换头像都要传几兆。
 *    **裁成正方形**（居中裁）而不是拉伸：拉伸会把脸压扁，而头像是圆的。
 * 2. **预览用的是本地对象 URL**，不落服务端：万一用户只是想看看效果、或者传之前
 *    改主意了，不该在桶里留一张别人不知道的图。
 *
 * 它挂在应用壳的账号菜单上（旧版由 `SideNav` 打开），外壳那边只需要
 * `<AvatarDialog open={...} name={...} url={...} onClose={...} />`。
 */
import { useEffect, useRef, useState, type ChangeEvent } from 'react'

import { clearAvatar, uploadAvatar } from '@/api/auth'
import { useSessionStore } from '@/lib/session'

import { notifyError, notifySuccess } from '../shared/toast'
import { Button } from '@/ui/button'
import { Avatar, Modal } from '../shared/composites'

/** 输出边长。256 在 2× 屏上看着也够，而文件通常只有几十 KB。 */
const OUTPUT_SIZE = 256

/**
 * 居中裁成正方形并缩到 `OUTPUT_SIZE`。
 *
 * 解码失败（少数格式浏览器解不开、文件其实是坏的）就抛出去——调用方把原因说给用户听。
 */
async function shrink(file: File): Promise<Blob> {
  if (!file.type.startsWith('image/')) throw new Error('请选一张图片')
  const bitmap = await createImageBitmap(file)
  const side = Math.min(bitmap.width, bitmap.height)
  const canvas = document.createElement('canvas')
  canvas.width = OUTPUT_SIZE
  canvas.height = OUTPUT_SIZE
  const context = canvas.getContext('2d')
  if (!context) throw new Error('这个浏览器画不出来（canvas 不可用）')
  context.drawImage(
    bitmap,
    (bitmap.width - side) / 2,
    (bitmap.height - side) / 2,
    side,
    side,
    0,
    0,
    OUTPUT_SIZE,
    OUTPUT_SIZE,
  )
  bitmap.close?.()
  return await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new Error('这张图转不成 PNG'))),
      'image/png',
    )
  })
}

export function AvatarDialog({
  open,
  name,
  url,
  onClose,
  onChanged,
}: {
  open: boolean
  name: string
  /** 当前头像链接（签名 URL）。空 = 用生成的那张。 */
  url: string
  onClose: () => void
  onChanged?: () => void
}) {
  const fileInput = useRef<HTMLInputElement | null>(null)
  /** 预览用的本地对象 URL（只负责显示）。 */
  const [preview, setPreview] = useState('')
  /** 缩好的那一份本体。**直接留着它，不从 URL 再 fetch 回来**：多一次 fetch 就多一个
   *  （没必要的）失败点，而这份 blob 就在手里。 */
  const [picked, setPicked] = useState<Blob | null>(null)
  const [busy, setBusy] = useState(false)

  // 关掉时把预览也丢掉：不丢的话下次打开会先闪一眼上一张
  useEffect(() => {
    if (open) return
    return () => {
      setPreview((current) => {
        if (current) URL.revokeObjectURL(current)
        return ''
      })
      setPicked(null)
    }
  }, [open])

  async function pick(event: ChangeEvent<HTMLInputElement>): Promise<void> {
    const chosen = event.target.files?.[0]
    if (fileInput.current) fileInput.current.value = ''
    if (!chosen) return
    try {
      const blob = await shrink(chosen)
      setPreview((current) => {
        if (current) URL.revokeObjectURL(current)
        return URL.createObjectURL(blob)
      })
      setPicked(blob)
    } catch (error) {
      notifyError(error instanceof Error ? error.message : '这张图读不出来')
    }
  }

  async function save(): Promise<void> {
    if (!picked || busy) return
    setBusy(true)
    try {
      // 名字带后缀是有意的：后端按**魔数**认格式，但文件名空着不像一次上传
      const account = await uploadAvatar(new File([picked], 'avatar.png', { type: 'image/png' }))
      // 换完头像界面上要立刻变：把接口返回的那份写回会话状态
      useSessionStore.setState({ currentUser: account })
      notifySuccess('头像已更新')
      onChanged?.()
      onClose()
    } catch (error) {
      notifyError(error instanceof Error ? error.message : '上传失败')
    } finally {
      setBusy(false)
    }
  }

  async function remove(): Promise<void> {
    if (busy) return
    setBusy(true)
    try {
      const account = await clearAvatar()
      useSessionStore.setState({ currentUser: account })
      notifySuccess('已去掉头像')
      onChanged?.()
      onClose()
    } catch (error) {
      notifyError(error instanceof Error ? error.message : '操作失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title="头像"
      onClose={onClose}
      size="md"
      footer={
        <>
          {url && (
            <span className="m-footer-left">
              <Button disabled={busy} onClick={() => void remove()}>
                去掉头像
              </Button>
            </span>
          )}
          <Button disabled={busy} onClick={() => fileInput.current?.click()}>
            {preview ? '重选' : '选择图片'}
          </Button>
          <Button disabled={!preview || busy} onClick={() => void save()}>
            {busy ? '上传中…' : '保存'}
          </Button>
        </>
      }
    >
      <div style={{ display: 'flex', justifyContent: 'center' }}>
        {/* 预览优先用刚选的那张（还没上传），否则显示当前生效的那张 */}
        {preview ? (
          <img
            src={preview}
            alt="新头像预览"
            style={{
              width: 96,
              height: 96,
              borderRadius: 'var(--radius-pill)',
              objectFit: 'cover',
            }}
          />
        ) : (
          <Avatar name={name} url={url || undefined} size={96} />
        )}
      </div>
      <p className="text-hint">一张方图最合适；上传时会裁成正方形（{OUTPUT_SIZE}px）。</p>
      <input
        ref={fileInput}
        className="m-local-input"
        type="file"
        accept="image/png,image/jpeg,image/gif,image/webp"
        aria-label="选择头像图片"
        onChange={(event) => void pick(event)}
      />
    </Modal>
  )
}
