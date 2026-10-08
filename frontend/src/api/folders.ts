/**
 * 知识库内目录接口（`/api/v1`，v13）。
 *
 * **单层目录**：不做父子嵌套（理由见后端 `services/folder.py`）。
 * 写操作需要读写权限；只读分享的成员能看目录，但建/改/删与移动都会被后端 403。
 */

import { request } from './client'
import type { components } from './schema'

/**
 * 目录：**契约来自后端的 OpenAPI**（见 `./schema.d.ts` 与 `conversations.ts` 头注
 * 里那三条约定：`Required<…>` 包一层、该收窄的显式收窄、写清为什么）。
 *
 * `document_count` 由后端 `GROUP BY` 一次算出，不是前端数的。
 */
export type Folder = Required<components['schemas']['FolderOut']>

export function listFolders(kbId: string): Promise<{ items: Folder[] }> {
  return request(`/knowledge-bases/${kbId}/folders`)
}

export function createFolder(kbId: string, name: string): Promise<Folder> {
  return request(`/knowledge-bases/${kbId}/folders`, {
    method: 'POST',
    body: JSON.stringify({ name }),
  })
}

export function renameFolder(folderId: string, name: string): Promise<Folder> {
  return request(`/folders/${folderId}`, {
    method: 'PATCH',
    body: JSON.stringify({ name }),
  })
}

/** 删目录。**目录非空时后端会拒绝（409）**，消息里带"还有几篇"。 */
export function deleteFolder(folderId: string): Promise<void> {
  return request(`/folders/${folderId}`, { method: 'DELETE' })
}
