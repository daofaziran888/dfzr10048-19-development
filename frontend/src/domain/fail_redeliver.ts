/** 失败重投领办的纯前端领域层：类型、阶段文案与结果判读，页面只负责渲染。 */

export type ClaimStage = '转码' | '转写'

export type AudioArtifact = {
  artifact_id: string
  batch_id: string
  shard_no: number
  shard_id: string
  job_id: string
  audio_bytes: number
  audio_ref: string
  success_flag: boolean
  usable: boolean
  status_text: string
  reason: string
}

export type AudioBinding = {
  开工回执: string
  阶段类别: ClaimStage
  批次: string
  分片序号: number
  音轨实物: string
  音轨来源作业: string
  音轨字节: number
  音轨引用: string
  音轨可用: boolean
}

export type ClaimReceipt = {
  receipt_id: string
  order_id: string
  batch_id: string
  shard_no: number
  stage: ClaimStage
  operator: string
  claimed_at: string
  fail_reason: string
  audio_usable: boolean
  audio_artifact_id: string | null
  audio_source_job: string | null
  audio_bytes: number
  note: string
  transferred_from: string | null
  started: boolean
  confirmed: boolean
  audio_binding: AudioBinding
}

export type RedeliverTodo = {
  order_id: string
  batch_id: string
  shard_no: number
  shard_id: string
  origin_stage: ClaimStage
  stage: ClaimStage
  fail_reason: string
  status: string
  active_stage: ClaimStage | null
  claimed_by: string | null
  open_receipt: ClaimReceipt | null
  transfer_count: number
  closed: boolean
  confirmed_artifact_id: string | null
  source_order_id: string | null
  receipts: ClaimReceipt[]
  failed_stage: ClaimStage
  claim_stage: ClaimStage
  usable_audio: AudioArtifact | null
  latest_artifact: AudioArtifact | null
  suggestion: string
}

export type TodoSummary = {
  转码: number
  转写: number
  合计: number
  执行中: number
}

export type TodoPage = {
  items: RedeliverTodo[]
  total: number
  page: number
  size: number
  stage: string
}

export type ClaimResult = {
  order: RedeliverTodo | null
  receipt: ClaimReceipt
  audio_binding: AudioBinding
}

export const STAGE_TABS: { key: '' | ClaimStage; label: string }[] = [
  { key: '', label: '全部阶段' },
  { key: '转码', label: '转码失败' },
  { key: '转写', label: '转写失败' },
]

/** 当前阶段可直接做的事：不同阶段/状态给不同按钮，避免现场误操作。 */
export function availableActions(todo: RedeliverTodo): string[] {
  if (todo.closed) return []
  if (!todo.active_stage) {
    return ['领办']
  }
  const actions: string[] = []
  if (!todo.open_receipt?.started) {
    actions.push('开工')
  }
  if (todo.active_stage === '转码') {
    if (todo.usable_audio) {
      if (!todo.open_receipt?.started) actions.push('原单转办')
      actions.push('结单改领')
    } else {
      // 原作业音轨迟到落盘时，先补登再决定转办还是结单。
      actions.push('补登音轨')
    }
  } else if (todo.active_stage === '转写' && todo.open_receipt?.audio_usable) {
    actions.push('确认产出')
  }
  return actions
}

/** 音轨状态一句话说明，现场先看这句再决定转办还是结单。 */
export function audioHint(todo: RedeliverTodo): string {
  if (todo.usable_audio) {
    return `可验证音轨 ${todo.usable_audio.artifact_id}（${todo.usable_audio.audio_bytes} 字节）`
  }
  if (todo.latest_artifact) {
    return todo.latest_artifact.status_text
  }
  return '尚无转码音轨'
}

export function canTransfer(todo: RedeliverTodo): boolean {
  return Boolean(todo.usable_audio) && todo.active_stage === '转码' && !todo.open_receipt?.started
}
