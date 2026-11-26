import { request } from './client'
import type { ActionResult } from '@/data/types'
import type {
  AudioArtifact,
  ClaimReceipt,
  ClaimResult,
  TodoPage,
  TodoSummary,
} from '@/domain/fail_redeliver'

export async function listRedeliverTodos(
  stage: string,
  page: number,
  size: number,
): Promise<TodoPage> {
  const query = new URLSearchParams({ page: String(page), size: String(size) })
  if (stage) query.set('stage', stage)
  const response = await request(`/api/fail_redeliver/todos?${query.toString()}`)
  if (!response.ok) throw new Error('领办待办读取失败')
  return await response.json()
}

export async function loadTodoSummary(): Promise<TodoSummary> {
  const response = await request('/api/fail_redeliver/todos/summary')
  if (!response.ok) throw new Error('待办汇总读取失败')
  return await response.json()
}

async function postAction(
  path: string,
  body: Record<string, unknown> = {},
): Promise<ActionResult> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return await response.json()
}

function entryOf<T>(result: ActionResult): T | null {
  return (result.entry as T | undefined) ?? null
}

/** 领办类接口统一返回 { order, receipt, audio_binding }，这里只取回执给页面展示。 */
export function receiptOf(result: ActionResult): ClaimReceipt | null {
  return entryOf<ClaimResult>(result)?.receipt ?? null
}

export async function claimOrder(orderId: string, operator: string): Promise<ActionResult> {
  return postAction(`/api/fail_redeliver/orders/${orderId}/claim`, { operator })
}

export async function startOrder(orderId: string): Promise<ActionResult> {
  return postAction(`/api/fail_redeliver/orders/${orderId}/start`)
}

export async function transferOrder(orderId: string, operator: string): Promise<ActionResult> {
  return postAction(`/api/fail_redeliver/orders/${orderId}/transfer`, { operator })
}

export async function closeAndReclaim(orderId: string, operator: string): Promise<ActionResult> {
  return postAction(`/api/fail_redeliver/orders/${orderId}/close-reclaim`, { operator })
}

export async function confirmOutput(
  orderId: string,
  artifactId: string | null,
  operator: string,
): Promise<ActionResult> {
  return postAction(`/api/fail_redeliver/orders/${orderId}/confirm`, {
    artifact_id: artifactId,
    operator,
  })
}

export async function registerArtifact(payload: {
  batch_id: string
  shard_no: number
  artifact_id?: string
  job_id?: string
  audio_bytes: number
  audio_ref: string
  success_flag: boolean
}): Promise<ActionResult> {
  return postAction('/api/fail_redeliver/artifacts', payload)
}

export function artifactOf(result: ActionResult): AudioArtifact | null {
  return entryOf<AudioArtifact>(result)
}

export { entryOf }
