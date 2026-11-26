<template>
  <section class="page claim-page" data-testid="claim-page">
    <header class="page-head">
      <div>
        <h2>失败分片领办待办</h2>
        <p class="page-desc">
          续做失败分片先看音轨：转码与转写分开领办，领到的开工回执会绑定实际投入的音轨。
        </p>
      </div>
      <div class="page-actions">
        <label class="operator-field">
          <span>领办人</span>
          <input v-model="operator" data-testid="operator" placeholder="填写值班人员" />
        </label>
        <button class="btn" data-testid="refresh" type="button" @click="reload">刷新待办</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">待办合计</span>
        <strong class="stat-value">{{ summary.合计 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">转码阶段</span>
        <strong class="stat-value">{{ summary.转码 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">转写阶段</span>
        <strong class="stat-value">{{ summary.转写 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">执行中</span>
        <strong class="stat-value">{{ summary.执行中 }}</strong>
      </article>
    </div>

    <nav class="stage-tabs" data-testid="stage-tabs">
      <button
        v-for="tab in STAGE_TABS"
        :key="tab.key"
        class="stage-tab"
        :class="{ active: stage === tab.key }"
        :data-testid="`stage-tab-${tab.key || 'all'}`"
        type="button"
        @click="switchStage(tab.key)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <table class="data-table">
      <thead>
        <tr>
          <th>分片序号</th>
          <th>工单</th>
          <th>关联分片</th>
          <th>失败阶段</th>
          <th>当前阶段</th>
          <th>原失败原因</th>
          <th>音轨情况</th>
          <th>开工回执</th>
          <th>状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="todo in rows"
          :key="todo.order_id"
          :data-testid="`todo-${todo.order_id}`"
          :data-stage="todo.active_stage || ''"
        >
          <td>{{ String(todo.shard_no).padStart(2, '0') }}</td>
          <td>
            {{ todo.order_id }}
            <span v-if="todo.source_order_id" class="hint">（原单 {{ todo.source_order_id }}）</span>
          </td>
          <td>{{ todo.shard_id }}</td>
          <td>{{ todo.origin_stage }}失败</td>
          <td>{{ todo.active_stage || todo.stage }}</td>
          <td class="reason-cell">{{ todo.fail_reason || '—' }}</td>
          <td :data-testid="`audio-${todo.order_id}`">{{ audioHint(todo) }}</td>
          <td class="receipt-cell">{{ receiptText(todo) }}</td>
          <td>{{ todo.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in availableActions(todo)"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${todo.order_id}-${action}`"
              @click="runAction(todo, action)"
            >
              {{ action }}
            </button>
            <span v-if="!availableActions(todo).length" class="hint">—</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td colspan="10" class="empty-state">当前阶段没有待办工单</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <div class="pager">
        <button class="btn" type="button" :disabled="page <= 1" data-testid="prev-page" @click="goPage(page - 1)">
          上一页
        </button>
        <span data-testid="page-info">第 {{ page }} / {{ pageCount }} 页 · 共 {{ total }} 条</span>
        <button
          class="btn"
          type="button"
          :disabled="page >= pageCount"
          data-testid="next-page"
          @click="goPage(page + 1)"
        >
          下一页
        </button>
        <select v-model.number="size" class="page-size" data-testid="page-size" @change="goPage(1)">
          <option :value="5">每页 5 条</option>
          <option :value="10">每页 10 条</option>
          <option :value="20">每页 20 条</option>
        </select>
      </div>
      <span v-if="message" data-testid="claim-message" :class="messageOk ? 'ok-text' : 'error-text'">
        {{ message }}
      </span>
    </footer>

    <section v-if="lastReceipt" class="receipt-panel" data-testid="receipt-panel">
      <h3>开工回执 {{ lastReceipt.receipt_id }}</h3>
      <dl class="receipt-grid">
        <div><dt>阶段</dt><dd>{{ lastReceipt.stage }}</dd></div>
        <div><dt>领办人</dt><dd>{{ lastReceipt.operator }}</dd></div>
        <div><dt>批次 / 分片</dt><dd>{{ lastReceipt.batch_id }} / {{ lastReceipt.shard_no }}</dd></div>
        <div><dt>音轨可用</dt><dd>{{ lastReceipt.audio_usable ? '是' : '否' }}</dd></div>
        <div><dt>音轨实物</dt><dd data-testid="receipt-audio">{{ lastReceipt.audio_binding.音轨实物 }}</dd></div>
        <div><dt>音轨来源作业</dt><dd>{{ lastReceipt.audio_binding.音轨来源作业 }}</dd></div>
        <div><dt>音轨字节</dt><dd>{{ lastReceipt.audio_binding.音轨字节 }}</dd></div>
        <div><dt>音轨引用</dt><dd class="mono">{{ lastReceipt.audio_binding.音轨引用 }}</dd></div>
        <div v-if="lastReceipt.transferred_from">
          <dt>转办来源</dt><dd>{{ lastReceipt.transferred_from }}</dd>
        </div>
      </dl>
      <p class="page-desc">{{ lastReceipt.note }}</p>
    </section>

    <div v-if="artifactModal" class="modal-backdrop" data-testid="artifact-modal">
      <form class="modal" @submit.prevent="submitArtifact">
        <h3>
          {{ artifactModal.mode === 'register' ? '补登音轨' : '结单改领' }} ·
          工单 {{ artifactModal.todo.order_id }}
        </h3>
        <p class="page-desc">
          {{
            artifactModal.mode === 'register'
              ? '原转码作业迟到落盘时在这里登记；登记后可选择原单转办或结单改领。'
              : '结单改领会新开一张转写工单并领办。已有可验证音轨可直接提交，否则先补登迟到音轨。'
          }}
        </p>
        <p class="page-desc">
          当前音轨：{{ audioHint(artifactModal.todo) }}
        </p>
        <label class="create-field">
          <span>批次</span>
          <input v-model="artifactValues.batch_id" data-testid="artifact-batch" required />
        </label>
        <label class="create-field">
          <span>分片序号</span>
          <input v-model.number="artifactValues.shard_no" type="number" min="1" data-testid="artifact-shard" required />
        </label>
        <label class="create-field">
          <span>音轨编号</span>
          <input v-model="artifactValues.artifact_id" data-testid="artifact-id" placeholder="可留空自动生成" />
        </label>
        <label class="create-field">
          <span>来源作业</span>
          <input v-model="artifactValues.job_id" data-testid="artifact-job" />
        </label>
        <label class="create-field">
          <span>录音字节</span>
          <input v-model.number="artifactValues.audio_bytes" type="number" min="0" data-testid="artifact-bytes" />
        </label>
        <label class="create-field">
          <span>音轨引用</span>
          <input v-model="artifactValues.audio_ref" data-testid="artifact-ref" />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="artifactModal = null">取消</button>
          <button class="btn primary" type="submit" data-testid="artifact-submit">
            {{ artifactModal.mode === 'register' ? '确认补登' : '结单并改领转写' }}
          </button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  claimOrder,
  closeAndReclaim,
  confirmOutput,
  listRedeliverTodos,
  loadTodoSummary,
  receiptOf,
  registerArtifact,
  startOrder,
  transferOrder,
} from '@/api/redeliver-stage'
import {
  STAGE_TABS,
  audioHint,
  availableActions,
} from '@/domain/fail_redeliver'
import type {
  ClaimReceipt,
  RedeliverTodo,
  TodoSummary,
} from '@/domain/fail_redeliver'

const operator = ref('值班员')
const stage = ref('')
const page = ref(1)
const size = ref(5)
const total = ref(0)
const rows = ref<RedeliverTodo[]>([])
const summary = ref<TodoSummary>({ 转码: 0, 转写: 0, 合计: 0, 执行中: 0 })
const message = ref('')
const messageOk = ref(true)
const lastReceipt = ref<ClaimReceipt | null>(null)
const artifactModal = ref<{ todo: RedeliverTodo; mode: 'register' | 'close-reclaim' } | null>(null)
const artifactValues = ref({
  batch_id: '',
  shard_no: 1,
  artifact_id: '',
  job_id: '',
  audio_bytes: 0,
  audio_ref: '',
})

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / size.value)))

function receiptText(todo: RedeliverTodo): string {
  const receipt = todo.open_receipt
  if (!receipt) return '—'
  return `${receipt.receipt_id}（${receipt.stage}）`
}

function setMessage(text: string, ok: boolean) {
  message.value = text
  messageOk.value = ok
}

function openArtifactModal(todo: RedeliverTodo, mode: 'register' | 'close-reclaim') {
  artifactModal.value = { todo, mode }
  artifactValues.value = {
    batch_id: todo.batch_id,
    shard_no: todo.shard_no,
    artifact_id: '',
    job_id: '',
    audio_bytes: 0,
    audio_ref: '',
  }
}

async function reload() {
  try {
    const [pageData, summaryData] = await Promise.all([
      listRedeliverTodos(stage.value, page.value, size.value),
      loadTodoSummary(),
    ])
    rows.value = pageData.items
    total.value = pageData.total
    summary.value = summaryData
    // 末页被清空时回退一页，避免停在空页。
    if (!pageData.items.length && page.value > 1) {
      page.value = Math.max(1, Math.ceil(pageData.total / size.value))
      if (page.value !== 1 || pageData.total > 0) {
        const retry = await listRedeliverTodos(stage.value, page.value, size.value)
        rows.value = retry.items
        total.value = retry.total
      }
    }
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '待办读取失败', false)
  }
}

function switchStage(next: string) {
  stage.value = next
  page.value = 1
  void reload()
}

function goPage(next: number) {
  page.value = Math.min(Math.max(1, next), pageCount.value)
  void reload()
}

async function runAction(todo: RedeliverTodo, action: string) {
  if (!operator.value.trim()) {
    setMessage('请先填写领办人', false)
    return
  }
  if (action === '补登音轨') {
    openArtifactModal(todo, 'register')
    setMessage('原作业音轨迟到落盘时，先补登再决定原单转办还是结单改领', true)
    return
  }
  if (action === '结单改领') {
    openArtifactModal(todo, 'close-reclaim')
    return
  }

  let result
  try {
    if (action === '领办') {
      result = await claimOrder(todo.order_id, operator.value.trim())
    } else if (action === '开工') {
      result = await startOrder(todo.order_id)
    } else if (action === '原单转办') {
      result = await transferOrder(todo.order_id, operator.value.trim())
    } else if (action === '确认产出') {
      result = await confirmOutput(
        todo.order_id,
        todo.open_receipt?.audio_artifact_id ?? null,
        operator.value.trim(),
      )
    } else {
      return
    }
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '操作失败', false)
    return
  }

  setMessage(result.message, result.ok)
  const receipt = receiptOf(result)
  if (receipt) lastReceipt.value = receipt
  await reload()
}

async function submitArtifact() {
  const modal = artifactModal.value
  if (!modal) return
  const values = artifactValues.value

  // 结单改领要求已有可验证音轨；先补登再走结单接口。
  const needRegister = modal.mode === 'close-reclaim' ? !modal.todo.usable_audio : true
  if (needRegister) {
    if (!values.audio_bytes || !values.audio_ref.trim()) {
      setMessage('录音字节与音轨引用都要填，仅有成功标记的音轨不能用于转写', false)
      return
    }
    try {
      const created = await registerArtifact({
        ...values,
        artifact_id: values.artifact_id || undefined,
        success_flag: true,
      })
      if (!created.ok) {
        setMessage(created.message, false)
        return
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '音轨登记失败', false)
      return
    }
  }

  if (modal.mode === 'register') {
    setMessage('音轨已补登，可继续选择原单转办或结单改领', true)
    artifactModal.value = null
    await reload()
    return
  }

  try {
    const result = await closeAndReclaim(modal.todo.order_id, operator.value.trim())
    setMessage(result.message, result.ok)
    const receipt = receiptOf(result)
    if (receipt) lastReceipt.value = receipt
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '结单改领失败', false)
  }
  artifactModal.value = null
  await reload()
}

onMounted(reload)
</script>
