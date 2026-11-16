<template>
  <section class="page delta-page" data-module="delta">
    <header class="page-head">
      <div>
        <h2>增量采集管理</h2>
        <p class="page-desc">维护增量游标、增量批次数据集与恢复文件：暂停批次按未结束处理，恢复文件沿用原游标与批次身份。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出增量采集清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reloadCursors">
      <label class="filter-item">
        <span>游标编号</span>
        <input v-model="keyword" placeholder="按游标编号检索，如 DELTA-208" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="statusFilter">
          <option value="">全部</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <h3 class="section-title">增量游标（暂停批次仍计入待处理）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>状态</th>
          <th>确认水位</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :class="{ 'paused-row': row.status === '已暂停' }">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span :class="['status-tag', row.status === '已暂停' ? 'status-paused' : '']">{{ row.status }}</span>
            <em v-if="row.status === '已暂停'" class="pending-hint">未结束·待处理</em>
          </td>
          <td>{{ row['确认水位'] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDrawer(row)">游标详情</button>
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无增量游标数据</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">增量待处理清单（恢复结果装载，未完成游标落这里）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>待处理编号</th>
          <th>批次编号</th>
          <th>游标编号</th>
          <th>未完成游标</th>
          <th>确认水位</th>
          <th>失败边界</th>
          <th>恢复文件</th>
          <th>批次状态</th>
          <th>来源</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in pendingRows" :key="item['待处理编号']">
          <td>{{ item['待处理编号'] }}</td>
          <td>{{ item['批次编号'] }}</td>
          <td>{{ item['游标编号'] }}</td>
          <td>{{ item['未完成游标'] }}</td>
          <td>{{ item['确认水位'] }}</td>
          <td>{{ item['失败边界起点'] }} ~ {{ item['失败边界终点'] ?? '—' }}</td>
          <td>{{ item['恢复文件编号'] }}</td>
          <td>{{ item['批次状态'] }}</td>
          <td>{{ item['来源'] }}</td>
        </tr>
        <tr v-if="!pendingRows.length">
          <td colspan="9" class="empty-state">暂无未完成游标</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">恢复文件（下载 / 上传 / 打包，文件清单按批次核对）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>文件编号</th>
          <th>文件名</th>
          <th>批次编号</th>
          <th>游标编号</th>
          <th>确认水位</th>
          <th>打包状态</th>
          <th>下载次数</th>
          <th>文件清单</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="file in recoveryFiles" :key="file['文件编号']">
          <td>{{ file['文件编号'] }}</td>
          <td>{{ file['文件名'] }}</td>
          <td>{{ file['批次编号'] }}</td>
          <td>{{ file['游标编号'] }}</td>
          <td>{{ file['确认水位'] }}</td>
          <td>{{ file['打包状态'] }}</td>
          <td>{{ file['下载次数'] }}</td>
          <td>
            <ul class="manifest-list">
              <li v-for="manifest in file['文件清单']" :key="manifest['文件名']">
                {{ manifest['文件名'] }}（{{ manifest['批次编号'] }}）
              </li>
            </ul>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="downloadFile(file)">下载</button>
            <button class="link" type="button" @click="importFile(file)">导入恢复</button>
            <button class="link" type="button" @click="packageFile(file)">打包</button>
          </td>
        </tr>
        <tr v-if="!recoveryFiles.length">
          <td colspan="9" class="empty-state">暂无恢复文件</td>
        </tr>
      </tbody>
    </table>

    <div class="upload-bar">
      <button class="btn" type="button" @click="uploadSeedFile">上传恢复文件 D90（绑定 B-DELTA-208-01）</button>
      <span class="hint">上传只登记文件身份并绑定原批次；恢复需再点「导入恢复」。</span>
    </div>

    <div v-if="drawerEntry" class="drawer-mask" @click.self="closeDrawer">
      <aside class="cursor-drawer">
        <header class="drawer-head">
          <h3>游标详情抽屉 · {{ drawerEntry['游标编号'] }}</h3>
          <button class="btn ghost" type="button" @click="closeDrawer">关闭</button>
        </header>
        <dl class="drawer-body">
          <template v-for="field in drawerFields" :key="field">
            <dt>{{ field }}</dt>
            <dd>{{ drawerEntry[field] ?? '—' }}</dd>
          </template>
          <dt>确认水位（唯一）</dt>
          <dd>{{ drawerEntry['确认水位'] ?? '—' }}</dd>
          <dt>批次编号</dt>
          <dd>{{ drawerEntry['批次编号'] ?? '—' }}</dd>
          <dt>批次状态</dt>
          <dd>{{ drawerEntry['批次状态'] ?? '—' }}</dd>
          <dt>恢复文件编号</dt>
          <dd>{{ drawerEntry['恢复文件编号'] ?? '—' }}</dd>
          <template v-if="drawerEntry['失败边界']">
            <dt>失败边界起点</dt>
            <dd>{{ drawerEntry['失败边界']['起点'] ?? '—' }}</dd>
            <dt>失败边界终点</dt>
            <dd>{{ drawerEntry['失败边界']['终点'] ?? '—' }}</dd>
            <dt>失败片段</dt>
            <dd>{{ (drawerEntry['失败边界']['失败片段'] ?? []).join('、') || '—' }}</dd>
          </template>
        </dl>
      </aside>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条增量游标 · {{ pendingRows.length }} 条未完成游标 · {{ recoveryFiles.length }} 个恢复文件</span>
      <span v-if="message" :class="messageOk ? 'ok-text' : 'error-text'">{{ message }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

/* eslint-disable @typescript-eslint/no-explicit-any */
type Row = Record<string, any>
type RecoveryFile = Record<string, any>

const ENDPOINT = '/api/delta'
const columns = ['游标编号', '目标端点', '增量字段', '当前水位', '回退步数', '批次上限', '去重窗口', '重跑开关']
const actions = ['提交校验', '启动增量', '暂停增量']
const statuses = ['草稿', '待校验', '运行中', '已暂停']
const drawerFields = ['目标端点', '增量字段', '当前水位', '回退步数', '批次上限', '去重窗口', '重跑开关']

const rows = ref<Row[]>([])
const total = ref(0)
const pendingRows = ref<Row[]>([])
const recoveryFiles = ref<RecoveryFile[]>([])
const keyword = ref('')
const statusFilter = ref('')
const message = ref('')
const messageOk = ref(true)
const drawerEntry = ref<Row | null>(null)

const stats = computed(() => [
  { label: '增量游标总数', value: rows.value.length },
  { label: '运行中游标', value: rows.value.filter((row) => row.status === '运行中').length },
  { label: '暂停未结束', value: rows.value.filter((row) => row.status === '已暂停').length },
  { label: '待处理游标', value: pendingRows.value.length },
])

function notify(text: string, ok = true) {
  message.value = text
  messageOk.value = ok
}

function resetFilters() {
  keyword.value = ''
  statusFilter.value = ''
  void reloadCursors()
}

async function reloadCursors() {
  const query = new URLSearchParams()
  if (keyword.value) query.set('keyword', keyword.value)
  if (statusFilter.value) query.set('status', statusFilter.value)
  const response = await request(`${ENDPOINT}?${query.toString()}`)
  if (!response.ok) throw new Error('增量游标列表读取失败')
  const payload = await response.json()
  rows.value = payload.items ?? []
  total.value = payload.total ?? rows.value.length
}

async function reloadPending() {
  const response = await request(`${ENDPOINT}/pending`)
  if (!response.ok) throw new Error('增量待处理清单读取失败')
  const payload = await response.json()
  pendingRows.value = payload.items ?? []
}

async function reloadFiles() {
  const response = await request(`${ENDPOINT}/recovery-files`)
  if (!response.ok) throw new Error('恢复文件清单读取失败')
  const payload = await response.json()
  recoveryFiles.value = payload.items ?? []
}

async function reloadAll() {
  try {
    await Promise.all([reloadCursors(), reloadPending(), reloadFiles()])
  } catch (error) {
    notify(error instanceof Error ? error.message : '增量采集数据读取失败', false)
  }
}

async function runAction(action: string, row: Row) {
  const response = await request(`${ENDPOINT}/${row.id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ values: { action } }),
  })
  const payload = await response.json()
  notify(payload.message ?? '动作已提交', Boolean(payload.ok))
  await reloadAll()
}

async function openDrawer(row: Row) {
  const response = await request(`${ENDPOINT}/${row.id}`)
  if (!response.ok) {
    notify('游标详情读取失败', false)
    return
  }
  drawerEntry.value = await response.json()
}

function closeDrawer() {
  drawerEntry.value = null
}

function downloadFile(file: RecoveryFile) {
  window.open(`${ENDPOINT}/recovery-files/${file['文件编号']}/download`, '_blank')
  setTimeout(reloadFiles, 400)
}

async function importFile(file: RecoveryFile) {
  const response = await request(`${ENDPOINT}/recovery-files/${file['文件编号']}/import`, { method: 'POST' })
  const payload = await response.json()
  notify(payload.message ?? '导入完成', Boolean(payload.ok))
  await reloadAll()
}

async function packageFile(file: RecoveryFile) {
  const response = await request(`${ENDPOINT}/recovery-files/${file['文件编号']}/package`, {
    method: 'POST',
    body: JSON.stringify({ values: {} }),
  })
  const payload = await response.json()
  notify(payload.message ?? '打包完成', Boolean(payload.ok))
  await reloadFiles()
}

async function uploadSeedFile() {
  const body = {
    values: {
      文件编号: 'D90',
      文件名: 'D90-DELTA-208-recovery.json',
      批次编号: 'B-DELTA-208-01',
      游标编号: 'DELTA-208',
      确认水位: '2026-11-16 09:40',
      文件格式: 'json',
      文件清单: [{ 文件名: 'D90-DELTA-208-recovery.json', 批次编号: 'B-DELTA-208-01', 大小字节: 512 }],
    },
  }
  const response = await request(`${ENDPOINT}/recovery-files/upload`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  const payload = await response.json()
  notify(payload.message ?? '上传完成', Boolean(payload.ok))
  await reloadFiles()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

onMounted(reloadAll)
</script>

<style scoped>
.delta-page .section-title {
  margin: 18px 0 8px;
  font-size: 15px;
}
.paused-row {
  background: #fff7ed;
}
.status-tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  background: #e2e8f0;
  font-size: 12px;
}
.status-paused {
  background: #fed7aa;
  color: #9a3412;
}
.pending-hint {
  margin-left: 6px;
  color: #b45309;
  font-size: 12px;
  font-style: normal;
}
.manifest-list {
  margin: 0;
  padding-left: 16px;
  font-size: 12px;
  color: var(--muted);
}
.upload-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
}
.upload-bar .hint {
  color: var(--muted);
  font-size: 12px;
}
.drawer-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  justify-content: flex-end;
  z-index: 20;
}
.cursor-drawer {
  width: 420px;
  height: 100%;
  background: #fff;
  padding: 16px 18px;
  overflow-y: auto;
  box-shadow: -6px 0 18px rgba(15, 23, 42, 0.18);
}
.drawer-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.drawer-body {
  display: grid;
  grid-template-columns: 130px 1fr;
  gap: 6px 10px;
  font-size: 13px;
}
.drawer-body dt {
  color: var(--muted);
}
.drawer-body dd {
  margin: 0;
}
.ok-text {
  color: #047857;
}
</style>
