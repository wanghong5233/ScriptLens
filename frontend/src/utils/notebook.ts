import dayjs from 'dayjs'
import {
  createFileOrDirectory,
  createWorkspace,
  fetchWorkspace,
  updateWorkspace,
} from '@/api/docStudio'

export const NOTEBOOK_WORKSPACE_ID = 'notebook'
export const NOTEBOOK_WORKSPACE_NAME = 'Notebook'
export const NOTEBOOK_USER_DIR = 'notes'
export const NOTEBOOK_AUTO_DIR = '_system/auto_notes'
export const NOTEBOOK_MAIN_FILE = `${NOTEBOOK_USER_DIR}/index.md`
export const NOTEBOOK_LOCKED_PATHS = [NOTEBOOK_AUTO_DIR]
export const NOTEBOOK_SYSTEM_WRITE_HEADER = 'X-Notebook-System-Write'
export const NOTEBOOK_DIR = NOTEBOOK_AUTO_DIR

const NOTEBOOK_WORKSPACE_CONFIG = {
  workspace_type: 'notebook',
  primary_format: 'markdown',
  supported_formats: ['markdown', 'plaintext'],
  main_file: NOTEBOOK_MAIN_FILE,
  notebook_auto_dir: NOTEBOOK_AUTO_DIR,
  notebook_locked_paths: NOTEBOOK_LOCKED_PATHS,
}

const NOTEBOOK_HOME_FILE_TEMPLATE = `# Notebook

欢迎使用 Notebook。

- 系统自动生成的研究笔记会保存在 \`${NOTEBOOK_AUTO_DIR}\`（只读目录）
- 你可以在 \`${NOTEBOOK_USER_DIR}\` 或其他自定义目录自由管理个人笔记
`

function stripQuotes(value: string) {
  return value.replace(/^['"]|['"]$/g, '').trim()
}

function slugify(value: string) {
  const base = value
    .toLowerCase()
    .replace(/[^a-z0-9\s-_]/g, '')
    .trim()
    .replace(/\s+/g, '-')
  return base || 'note'
}

export function buildNoteFileName(title: string) {
  const stamp = dayjs().format('YYYYMMDD_HHmmss')
  const slug = slugify(stripQuotes(title || 'note'))
  return `${stamp}_${slug}.md`
}

function getErrorStatus(error: any) {
  return Number(error?.response?.status || 0)
}

function getNotebookSystemWriteOptions() {
  return {
    loading: false,
    errorToast: false,
    headers: {
      [NOTEBOOK_SYSTEM_WRITE_HEADER]: '1',
    },
  }
}

async function ensureNotebookStructure() {
  const pathsToCreate = [
    { path: NOTEBOOK_USER_DIR, type: 'directory' as const },
    { path: NOTEBOOK_AUTO_DIR, type: 'directory' as const },
    {
      path: NOTEBOOK_MAIN_FILE,
      type: 'file' as const,
      content: NOTEBOOK_HOME_FILE_TEMPLATE,
    },
  ]
  for (const item of pathsToCreate) {
    try {
      await createFileOrDirectory(
        {
          workspaceId: NOTEBOOK_WORKSPACE_ID,
          path: item.path,
          type: item.type,
          content: item.type === 'file' ? item.content : undefined,
        },
        getNotebookSystemWriteOptions(),
      )
    } catch (error: any) {
      if (getErrorStatus(error) !== 400) throw error
    }
  }
}

function shouldSyncNotebookConfig(config: Record<string, any>) {
  const workspaceType = String(config?.workspace_type || config?.workspaceType || '')
    .trim()
    .toLowerCase()
  if (workspaceType !== 'notebook') return true

  const primaryFormat = String(config?.primary_format || config?.primaryFormat || '')
    .trim()
    .toLowerCase()
  if (primaryFormat !== 'markdown') return true

  const mainFile = String(config?.main_file || config?.mainFile || '')
    .trim()
    .toLowerCase()
  if (mainFile !== NOTEBOOK_MAIN_FILE.toLowerCase()) return true

  const autoDir = String(config?.notebook_auto_dir || config?.notebookAutoDir || '')
    .trim()
    .toLowerCase()
  if (autoDir !== NOTEBOOK_AUTO_DIR.toLowerCase()) return true

  const rawLockedPaths = config?.notebook_locked_paths || config?.notebookLockedPaths
  const normalizedLockedPaths = Array.isArray(rawLockedPaths)
    ? rawLockedPaths
        .map((item: any) => String(item || '').trim().toLowerCase())
        .filter(Boolean)
    : []
  return !normalizedLockedPaths.includes(NOTEBOOK_AUTO_DIR.toLowerCase())
}

export async function ensureNotebookWorkspace() {
  let workspaceConfig: Record<string, any> = {}
  try {
    const workspace = await fetchWorkspace({ workspaceId: NOTEBOOK_WORKSPACE_ID })
    workspaceConfig = workspace.config || {}
  } catch (error: any) {
    if (getErrorStatus(error) !== 404) throw error
    await createWorkspace({
      name: NOTEBOOK_WORKSPACE_NAME,
      workspaceId: NOTEBOOK_WORKSPACE_ID,
      config: NOTEBOOK_WORKSPACE_CONFIG,
    })
    workspaceConfig = NOTEBOOK_WORKSPACE_CONFIG
  }

  if (shouldSyncNotebookConfig(workspaceConfig)) {
    await updateWorkspace({
      workspaceId: NOTEBOOK_WORKSPACE_ID,
      config: {
        ...workspaceConfig,
        ...NOTEBOOK_WORKSPACE_CONFIG,
      },
    })
  }
  await ensureNotebookStructure()
}

export async function ensureNotebookDirectory() {
  await ensureNotebookWorkspace()
}

export async function createNotebookNoteFile(content: string, title: string) {
  await ensureNotebookWorkspace()
  const baseName = buildNoteFileName(title)
  let targetPath = `${NOTEBOOK_AUTO_DIR}/${baseName}`
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const suffix = attempt ? `_${attempt}` : ''
    targetPath = `${NOTEBOOK_AUTO_DIR}/${baseName.replace('.md', `${suffix}.md`)}`
    try {
      await createFileOrDirectory(
        {
          workspaceId: NOTEBOOK_WORKSPACE_ID,
          path: targetPath,
          type: 'file',
          content,
        },
        getNotebookSystemWriteOptions(),
      )
      return targetPath
    } catch (error: any) {
      if (getErrorStatus(error) !== 400) throw error
    }
  }
  throw new Error('Notebook file name conflict')
}
