const { createApp, ref, computed, onMounted, onUnmounted, nextTick } = Vue

createApp({
  delimiters: ['[[', ']]'],
  components: {
    'sources-view': SourcesView,
    'settings-view': SettingsView,
    'logs-view': LogsView
  },
  setup() {
    const tasks = ref([])
    const sources = ref([])
    const filter = ref(null)
    const tab = ref('tasks')
    const detailTask = ref(null)  // legacy, kept for backward compatibility
    const pollMsg = ref('')
    const polling = ref(false)
    const starting = ref(false)
    const batchLoading = ref(false)  // 批量操作加载状态
    const schedulerConfig = ref({rss_cron: '0 4 * * *', rss_enabled: 'true'})
    const downloadConfig = ref({download_dir: '', temp_dir: '', max_concurrent: '2', thread_count: '8', move_to_nas: 'true'})
    const proxyConfig = ref({enabled: 'false', type: 'http', host: '', port: '7890', username: '', password: ''})
    const logConfig = ref({log_level: 'INFO', log_path: ''})
    const logSaving = ref(false)
    const logSaved = ref('')
    const newSource = ref({name:'', url:'', feed_type: 'webpage', poll_cron: '0 */8 * * *'})
    const deleteTarget = ref(null)
    const editingSource = ref(null)
    const showAddModal = ref(false)
    const showPollModal = ref(false)
    const addUrl = ref('')
    const adding = ref(false)
    const addMsg = ref('')
    const addMode = ref('url')
    const addM3u8Url = ref('')
    const addM3u8Name = ref('')
    const addM3u8Headers = ref('')
    const addDownloadDir = ref('')
    const configSaved = ref('')
    const configSaving = ref(false)
    const proxySaved = ref('')
    const proxySaving = ref(false)
    const searchQuery = ref('')
    const sortBy = ref('default')  // default | created_asc | created_desc | name | status
    const detailModal = ref(null)   // task object for detail modal
    let pollTimer = null
    let taskSource = null
    let systemStatsSource = null
    let systemLogsSource = null

    // 系统资源状态（CPU/内存）
    const systemStats = ref(null)
    // 系统日志
    const systemLogs = ref([])

    const counts = computed(() => {
      let a=0,c=0,f=0,w=0,s=0,d=0,m=0,mv=0
      for (const t of tasks.value) {
        if (t.stage==='downloading') d++
        else if (t.stage==='merging') m++
        else if (t.stage==='moving') mv++
        else if (t.stage==='completed') c++
        else if (t.stage==='failed') f++
        else if (t.stage==='waiting') w++
        else if (t.stage==='stopped') s++
      }
      return {active:d+m+mv, completed:c, failed:f, waiting:w, stopped:s, downloading:d, merging:m, moving:mv}
    })
    const activeCount = computed(() => counts.value.active)
    const completedCount = computed(() => counts.value.completed)
    const failedCount = computed(() => counts.value.failed)
    const waitingCount = computed(() => counts.value.waiting)
    const stoppedCount = computed(() => counts.value.stopped)
    const downloadingCount = computed(() => counts.value.downloading)
    const mergingCount = computed(() => counts.value.merging)
    const movingCount = computed(() => counts.value.moving)
    const totalSpeed = computed(() => {
      let total = 0
      for (const t of tasks.value) {
        if (t.stage === 'downloading' && t.speed) {
          const s = t.speed.trim()
          // 支持 MB/s, KB/s, GB/s, B/s (yt-dlp) 和 MiB/s, KiB/s (二进制) 以及 MBps, KBps (旧格式)
          if (s.endsWith('MB/s') || s.endsWith('MBps') || s.endsWith('MiB/s')) total += parseFloat(s) || 0
          else if (s.endsWith('KB/s') || s.endsWith('KBps') || s.endsWith('KiB/s')) total += (parseFloat(s) || 0) / 1024
          else if (s.endsWith('GB/s') || s.endsWith('GBps') || s.endsWith('GiB/s')) total += (parseFloat(s) || 0) * 1024
        }
      }
      return total
    })
    const selected = ref([])  // array of selected task IDs
    const savedMode = localStorage.getItem('dl_view_mode')
    const viewMode = ref(savedMode === 'list' ? 'list' : 'grid')  // 'grid' | 'list'
    const pageSize = ref(36)
    const page = ref(1)
    const selectedCount = computed(() => selected.value.length)
    function relativeTime(dateStr) {
      if (!dateStr) return ''
      const d = new Date(dateStr.replace('Z', '+00:00'))
      if (isNaN(d.getTime())) return dateStr
      const now = Date.now()
      const diff = Math.floor((now - d.getTime()) / 1000)
      if (diff < 0) return '刚刚'
      if (diff < 60) return `${diff}秒前`
      if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`
      if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`
      if (diff < 2592000) return `${Math.floor(diff / 86400)}天前`
      // 超过30天显示具体日期
      return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
    }

    const stagePriority = {downloading:0, merging:1, moving:2, waiting:3, completed:4, failed:5, stopped:6}

    const allSorted = computed(() => {
      // 1. 按 stage 过滤
      let list = filter.value ? tasks.value.filter(t => t.stage === filter.value) : [...tasks.value]
      // 2. 模糊搜索
      const q = searchQuery.value.trim().toLowerCase()
      if (q) {
        list = list.filter(t => (t.name || '').toLowerCase().includes(q) || (t.id || '').toLowerCase().includes(q))
      }
      // 3. 排序
      if (sortBy.value === 'created_asc') {
        list.sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''))
      } else if (sortBy.value === 'created_desc') {
        list.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
      } else if (sortBy.value === 'name') {
        list.sort((a, b) => (a.name || '').localeCompare(b.name || '', 'zh'))
      } else if (sortBy.value === 'status') {
        list.sort((a, b) => {
          const pa = stagePriority[a.stage] ?? 7
          const pb = stagePriority[b.stage] ?? 7
          return pa - pb
        })
      } else {
        // default: 阶段优先级 + 时间倒序
        list.sort((a, b) => {
          const pa = stagePriority[a.stage] ?? 7
          const pb = stagePriority[b.stage] ?? 7
          if (pa !== pb) return pa - pb
          return (b.created_at || '').localeCompare(a.created_at || '')
        })
      }
      return list
    })
    const showPagination = computed(() => allSorted.value && allSorted.value.length > pageSize.value)
    const totalPages = computed(() => allSorted.value ? Math.ceil(allSorted.value.length / pageSize.value) : 1)
    const filteredTasks = computed(() => {
      const start = (page.value - 1) * pageSize.value
      return allSorted.value.slice(start, start + pageSize.value)
    })
    // 当筛选或页码改变时，如果当前页超出总页数，回到第一页
    function goPage(p) { page.value = Math.max(1, Math.min(p, totalPages.value)) }
    function setPageSize(s) { pageSize.value = s; page.value = 1 }


    const allSelected = computed(() => filteredTasks.value.length > 0 && filteredTasks.value.every(t => selected.value.includes(t.id)))
    // 点击全选时只选择当前页的任务
    const canBatchStart = computed(() => filteredTasks.value.some(t => selected.value.includes(t.id) && (t.stage === 'waiting' || t.stage === 'stopped')))
    const canBatchStop = computed(() => filteredTasks.value.some(t => selected.value.includes(t.id) && ['downloading','merging','moving'].includes(t.stage)))
    const canBatchRetry = computed(() => filteredTasks.value.some(t => selected.value.includes(t.id) && ['failed','completed'].includes(t.stage)))

    function toggleSelect(t) {
      const idx = selected.value.indexOf(t.id)
      if (idx >= 0) selected.value.splice(idx, 1)
      else selected.value.push(t.id)
    }
    function toggleSelectAll() {
      if (allSelected.value) {
        // 取消当前页所有
        filteredTasks.value.forEach(t => {
          const idx = selected.value.indexOf(t.id)
          if (idx >= 0) selected.value.splice(idx, 1)
        })
      } else {
        // 选中当前页所有
        filteredTasks.value.forEach(t => {
          if (!selected.value.includes(t.id)) selected.value.push(t.id)
        })
      }
    }
    async function batchStart() {
      if (batchLoading.value) return
      if (selected.value.length === 0) return
      batchLoading.value = true
      try {
        const res = await fetch('/api/tasks/batch/start', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ids: selected.value})
        })
        const data = await res.json()
        console.log('批量开始:', data)
      } catch(e) {
        console.error('批量开始失败:', e)
      }
      selected.value = []
      batchLoading.value = false
      await fetchTasks()
    }
    async function batchStop() {
      if (batchLoading.value) return
      if (selected.value.length === 0) return
      batchLoading.value = true
      try {
        const res = await fetch('/api/tasks/batch/stop', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ids: selected.value})
        })
        const data = await res.json()
        console.log('批量暂停:', data)
      } catch(e) {
        console.error('批量暂停失败:', e)
      }
      selected.value = []
      batchLoading.value = false
      await fetchTasks()
    }
    function toggleViewMode() {
      viewMode.value = viewMode.value === 'grid' ? 'list' : 'grid'
      localStorage.setItem('dl_view_mode', viewMode.value)
    }
    async function batchRetry() {
      if (batchLoading.value) return
      if (selected.value.length === 0) return
      batchLoading.value = true
      try {
        const res = await fetch('/api/tasks/batch/retry', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ids: selected.value})
        })
        const data = await res.json()
        console.log('批量重试:', data)
      } catch(e) {
        console.error('批量重试失败:', e)
      }
      selected.value = []
      batchLoading.value = false
      await fetchTasks()
    }
    async function batchDelete() {
      if (batchLoading.value) return
      if (!confirm(`确定要删除选中的 ${selectedCount.value} 个任务？`)) return
      batchLoading.value = true
      try {
        const res = await fetch('/api/tasks/batch/delete', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ids: selected.value})
        })
        const data = await res.json()
        if (!res.ok) {
          alert('批量删除失败: ' + (data.detail || data.message || res.status))
        } else if (data.failed && data.failed.length > 0) {
          alert(`批量删除完成：成功 ${data.success.length} 个，失败 ${data.failed.length} 个`)
        }
      } catch(e) {
        alert('批量删除失败: ' + (e.message || e))
      }
      selected.value = []
      batchLoading.value = false
      await fetchTasks()
    }

        function stageLabel(s) {
      const m = {waiting:'等待', downloading:'下载中', merging:'合并中', moving:'转移中', completed:'已完成', failed:'失败', stopped:'已停止'}
      return m[s] || s
    }

    async function fetchTasks() {
      try { const r = await fetch('/api/tasks'); const d = await r.json(); tasks.value = d.list || [] } catch(e) {}
    }
    async function fetchSources() {
      try { const r = await fetch('/api/sources'); const d = await r.json(); sources.value = d.list || [] } catch(e) {}
    }
    async function fetchScheduler() {
      try { const r = await fetch('/api/scheduler'); const d = await r.json(); schedulerConfig.value = d.data || {} } catch(e) {}
    }
    async function fetchConfig() {
      try { const r = await fetch('/api/config'); const d = await r.json();
        const data = d.data || {}
        schedulerConfig.value = {rss_cron: data.rss_cron || '0 4 * * *', rss_enabled: data.rss_enabled || 'true'}
        downloadConfig.value = {download_dir: data.download_dir || '', temp_dir: data.temp_dir || '', max_concurrent: data.max_concurrent || '2', thread_count: data.thread_count || '8', move_to_nas: data.move_to_nas || 'true'}
        proxyConfig.value = {enabled: data.enabled || 'false', type: data.type || 'http', host: data.host || '', port: data.port || '7890', username: data.username || '', password: data.password || ''}
        logConfig.value = {log_level: data.log_level || 'INFO', log_path: data.log_path || ''}
      } catch(e) {}
    }

    async function saveProxyConfig() {
      proxySaving.value = true
      proxySaved.value = ''
      try {
        const r = await fetch('/api/proxy', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(proxyConfig.value)
        })
        const d = await r.json()
        proxySaved.value = d.message || '已保存'
        setTimeout(() => proxySaved.value = '', 3000)
      } catch(e) {
        alert('保存代理设置失败: ' + (e.message || e))
        console.error(e)
      }
      proxySaving.value = false
    }

    async function saveLogConfig() {
      logSaving.value = true
      logSaved.value = ''
      try {
        const r = await fetch('/api/log-config', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(logConfig.value)
        })
        const d = await r.json()
        logSaved.value = d.message || '已保存'
        setTimeout(() => logSaved.value = '', 3000)
      } catch(e) {
        alert('保存日志设置失败: ' + (e.message || e))
        console.error(e)
      }
      logSaving.value = false
    }

    async function startAll() {
      starting.value = true
      try {
        const r = await fetch('/api/start-waiting', {method:'POST'})
        await fetchTasks()
      } catch(e) {}
      starting.value = false
    }

    async function saveDownloadConfig() {
      const mc = Math.max(1, Math.min(99, parseInt(downloadConfig.value.max_concurrent) || 2))
      const tc = Math.max(1, Math.min(16, parseInt(downloadConfig.value.thread_count) || 8))
      downloadConfig.value.max_concurrent = mc
      downloadConfig.value.thread_count = tc
      configSaving.value = true
      configSaved.value = ''
      try {
        const body = { max_concurrent: mc, thread_count: tc, move_to_nas: downloadConfig.value.move_to_nas }
        if (downloadConfig.value.download_dir) body.download_dir = downloadConfig.value.download_dir
        if (downloadConfig.value.temp_dir) body.temp_dir = downloadConfig.value.temp_dir
        const r = await fetch('/api/config/apply', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify(body)
        })
        const d = await r.json()
        configSaved.value = d.message || '已保存'
        setTimeout(() => configSaved.value = '', 3000)
        if (d.stopped > 0) await fetchTasks()
      } catch(e) { alert('保存设置失败: ' + e.message); console.error(e) }
      configSaving.value = false
    }

    async function saveSchedulerConfig() {
      try {
        await fetch(`/api/scheduler?key=rss_cron&value=${encodeURIComponent(schedulerConfig.value.rss_cron)}`, {method:'POST'})
        await fetch(`/api/scheduler?key=rss_enabled&value=${encodeURIComponent(schedulerConfig.value.rss_enabled)}`, {method:'POST'})
      } catch(e) { console.error('save scheduler failed', e) }
    }

    async function retryTask(t) {
      const isRedo = t.stage === 'completed'
      const msg = isRedo
        ? `确定要重新下载任务 ${t.id}？这将删除已下载的文件并重新下载。`
        : `确定要重试任务 ${t.id}？(已重试 ${t.retry_count || 0}/3 次)`
      if (!confirm(msg)) return
      try {
        const r = await fetch(`/api/tasks/${t.id}/retry`, {method:'POST'})
        const d = await r.json()
        if (d.max_reached) { alert('重试次数已达上限，请删除任务'); return }
        await fetchTasks()
      } catch(e) { console.error('retry failed', e) }
    }

    async function startTask(t) {
      try {
        const r = await fetch(`/api/tasks/${t.id}/start`, {method:'POST'})
        const d = await r.json()
        if (d.message) pollMsg.value = d.message
        setTimeout(() => { if (pollMsg.value === d.message) pollMsg.value = '' }, 3000)
        await fetchTasks()
      } catch(e) { alert('启动失败: ' + (e.message || e)); console.error(e) }
    }

    async function stopTask(t) {
      if (!confirm(`确定要暂停任务 ${t.id}？`)) return
      try {
        await fetch(`/api/tasks/${t.id}/stop`, {method:'POST'})
        await fetchTasks()
      } catch(e) { alert('暂停失败: ' + (e.message || e)); console.error(e) }
    }


    async function pollNow() {
      // 旧方法保留兼容，实际使用 pollSourceById
      polling.value = true
      pollMsg.value = ''
      try {
        const r = await fetch('/api/rss/poll', {method:'POST'})
        const d = await r.json()
        pollMsg.value = d.message || ''
        setTimeout(() => pollMsg.value = '', 5000)
        await fetchTasks()
      } catch(e) { pollMsg.value = 'RSS 轮询失败' }
      polling.value = false
    }

    async function pollSourceById(sourceId) {
      showPollModal.value = false
      polling.value = true
      pollMsg.value = ''
      try {
        const r = await fetch(`/api/sources/${sourceId}/poll`, {method:'POST'})
        const d = await r.json()
        pollMsg.value = d.message || ''
        setTimeout(() => pollMsg.value = '', 5000)
        await fetchTasks()
      } catch(e) { pollMsg.value = '轮询失败' }
      polling.value = false
    }

    async function addSource() {
      if (!newSource.value.name || !newSource.value.url) return
      await fetch('/api/sources', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(newSource.value)})
      newSource.value = {name:'', url:'', feed_type:'webpage', poll_cron: '0 4 * * *'}
      await fetchSources()
    }

    function editSource(s) {
      editingSource.value = {
        id: s.id, name: s.name, url: s.url, feed_type: s.feed_type, enabled: s.enabled,
        poll_cron: s.poll_cron || '0 4 * * *',
        page_url_pattern: s.page_url_pattern || '',
        title_selector: s.title_selector || '',
        m3u8_selector: s.m3u8_selector || '',
        video_id_pattern: s.video_id_pattern || '',
        referer: s.referer || '',
        headers: s.headers || '',
        key_selector: s.key_selector || '',
        iv_selector: s.iv_selector || '',
        refresh_url_pattern: s.refresh_url_pattern || '',
      }
    }

    async function saveSource() {
      if (!editingSource.value) return
      await fetch(`/api/sources/${editingSource.value.id}`, {
        method: 'PUT',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(editingSource.value)
      })
      editingSource.value = null
      await fetchSources()
    }

    async function toggleSource(s) {
      await fetch(`/api/sources/${s.id}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled: !s.enabled})})
      await fetchSources()
    }

    async function pollSource(s) {
      try {
        const r = await fetch(`/api/sources/${s.id}/poll`, {method:'POST'})
        const d = await r.json()
        pollMsg.value = d.message || ''
        setTimeout(() => { if (pollMsg.value === d.message) pollMsg.value = '' }, 5000)
        await fetchTasks()
      } catch(e) { pollMsg.value = '轮询失败' }
    }

    async function delSource(id) {
      if (!confirm('确认删除该订阅源?')) return
      await fetch(`/api/sources/${id}`, {method:'DELETE'})
      await fetchSources()
    }

    async function toggleScheduler() {
      const newVal = schedulerConfig.value.rss_enabled === 'true' ? 'false' : 'true'
      await fetch(`/api/scheduler?key=rss_enabled&value=${newVal}`, {method:'POST'})
      schedulerConfig.value.rss_enabled = newVal
    }

    async function updateScheduler() {
      await fetch(`/api/scheduler?key=rss_cron&value=${encodeURIComponent(schedulerConfig.value.rss_cron)}`, {method:'POST'})
    }

    async function deleteTask(t) {
      deleteTarget.value = t
    }

    async function confirmDelete() {
      const t = deleteTarget.value
      deleteTarget.value = null
      if (!t) return
      // 先停止(如果正在运行)
      if (['downloading','merging','moving'].includes(t.stage)) {
        await fetch(`/api/tasks/${t.id}/stop`, {method:'POST'}).catch(()=>{})
      }
      try {
        const res = await fetch(`/api/tasks/${t.id}`, {method:'DELETE'})
        if (!res.ok) {
          const d = await res.json().catch(() => ({}))
          alert('删除失败: ' + (d.detail || d.message || res.status))
          return
        }
      } catch(e) {
        alert('删除失败: ' + (e.message || e))
        return
      }
      await fetchTasks()
    }

    // 搜索改变时回到第一页
    function onSearchChange() { page.value = 1 }

        // 历史日志相关
    const detailTab = ref('info')
    const historyLogLines = ref([])
    const historyLogTotal = ref(0)
    const historyLogPage = ref(1)
    const historyLogPageSize = ref(100)
    const historyLogSearch = ref('')
    const historyLogLevel = ref('all')

    function getLogLineClass(line) {
      if (line.includes('[ERROR]') || line.includes('失败')) return 'log-error'
      if (line.includes('[WARN]') || line.includes('警告')) return 'log-warn'
      if (line.includes('[INFO]')) return 'log-info'
      if (line.includes('[DEBUG]')) return 'log-debug'
      return 'log-default'
    }

    const filteredHistoryLogLines = computed(() => {
      if (historyLogLevel.value === 'all') return historyLogLines.value
      return historyLogLines.value.filter(line => {
        if (historyLogLevel.value === 'error') return line.includes('[ERROR]') || line.includes('失败')
        if (historyLogLevel.value === 'warn') return line.includes('[WARN]') || line.includes('警告')
        if (historyLogLevel.value === 'info') return line.includes('[INFO]')
        if (historyLogLevel.value === 'debug') return line.includes('[DEBUG]')
        return true
      })
    })

    async function loadHistoryLog() {
      if (!detailModal.value) return
      try {
        const params = new URLSearchParams({page: historyLogPage.value, page_size: historyLogPageSize.value})
        if (historyLogSearch.value) params.set('search', historyLogSearch.value)
        const r = await fetch(`/api/tasks/${detailModal.value.id}/logs/history?${params}`)
        const d = await r.json()
        historyLogLines.value = d.list || []
        historyLogTotal.value = d.total || 0
      } catch(e) { historyLogLines.value = [] }
    }

    function showDetail(t) {
      detailModal.value = t
      detailTab.value = 'info'
      historyLogPage.value = 1
      historyLogSearch.value = ''
      historyLogLevel.value = 'all'
      historyLogLines.value = []
      // 后台预加载历史日志，切换到日志Tab时数据已就绪
      loadHistoryLog()
    }
    function closeDetail() {
      detailModal.value = null
    }

    function clearSystemLogs() {
      systemLogs.value = []
    }

    const canAddVideo = computed(() => {
      if (adding.value) return false
      if (addMode.value === 'url') return !!addUrl.value
      return !!addM3u8Url.value
    })

    async function doAddVideo() {
      if (!canAddVideo.value) return
      adding.value = true
      addMsg.value = '正在解析视频页面，请稍候...（可能需要 10-20 秒）'
      try {
        let r
        if (addMode.value === 'url') {
          r = await fetch('/api/tasks/from-url', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({url: addUrl.value, download_dir: addDownloadDir.value})
          })
        } else {
          r = await fetch('/api/tasks/from-m3u8', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
              m3u8_url: addM3u8Url.value,
              name: addM3u8Name.value,
              headers: addM3u8Headers.value,
              download_dir: addDownloadDir.value
            })
          })
        }
        const d = await r.json()
        if (!r.ok) throw new Error((d.detail || d.message || '添加失败').replace(/^Error:\s*/, ''))
        addMsg.value = '添加成功！'
        addUrl.value = ''
        addM3u8Url.value = ''
        addM3u8Name.value = ''
        addM3u8Headers.value = ''
        addDownloadDir.value = ''
        await fetchTasks()
        setTimeout(() => { showAddModal.value = false; addMsg.value = '' }, 1500)
      } catch(e) {
        addMsg.value = '添加失败: ' + e.message
      }
      adding.value = false
    }

    onMounted(() => {
      fetchSources()
      fetchScheduler()
      fetchConfig()
      // 用 SSE 替代轮询：服务器主动推送任务变更
      function connectTaskEvents() {
        if (taskSource) { taskSource.close() }
        const es = new EventSource('/api/tasks/events')
        taskSource = es
        es.onmessage = (e) => {
          try {
            const data = JSON.parse(e.data)
            const newList = data.list || []
            // 按 id 合并更新，避免全量替换导致进度条闪烁
            const map = new Map(newList.map(t => [t.id, t]))
            const oldIds = new Set(tasks.value.map(t => t.id))
            const newIds = new Set(map.keys())
            // 有增删时直接替换，只有纯更新时合并
            const hasAddRemove = oldIds.size !== newIds.size || [...oldIds].some(id => !newIds.has(id))
            if (hasAddRemove) {
              tasks.value = newList
            } else {
              tasks.value = tasks.value.map(old => {
                const updated = map.get(old.id)
                if (!updated) return old
                // 只在关键字段变化时替换对象引用，否则复用旧对象避免无意义重渲染
                if (old.status !== updated.status || old.stage !== updated.stage ||
                    old.progress !== updated.progress || old.speed !== updated.speed ||
                    old.segments !== updated.segments || old.error !== updated.error ||
                    old.move_speed !== updated.move_speed || old.move_elapsed !== updated.move_elapsed ||
                    old.file !== updated.file || old.final_path !== updated.final_path ||
                    old.priority !== updated.priority || old.retry_count !== updated.retry_count) {
                  return updated
                }
                return old
              })
            }
          } catch(err) {}
        }
        es.onerror = () => {
          // EventSource 会自动重连，这里可以加日志
          console.warn('[SSE] 连接断开，自动重连中...')
        }
      }
      connectTaskEvents()

      // 连接系统资源 SSE
      function connectSystemStats() {
        if (systemStatsSource) { systemStatsSource.close() }
        const es = new EventSource('/api/system/stats/stream')
        systemStatsSource = es
        es.onmessage = (e) => {
          try {
            systemStats.value = JSON.parse(e.data)
          } catch(err) {}
        }
        es.onerror = () => {
          console.warn('[SSE System] 连接断开')
        }
      }
      connectSystemStats()

      // 连接系统日志 SSE
      function connectSystemLogs() {
        if (systemLogsSource) { systemLogsSource.close() }
        const es = new EventSource('/api/system/logs')
        systemLogsSource = es
        es.onmessage = (e) => {
          systemLogs.value.push(e.data)
          // 限制日志条数
          if (systemLogs.value.length > 1000) {
            systemLogs.value = systemLogs.value.slice(-1000)
          }
        }
        es.onerror = () => {
          console.warn('[SSE System Logs] 连接断开')
        }
      }
      connectSystemLogs()
    })
    onUnmounted(() => {
      clearInterval(pollTimer)
      if (taskSource) taskSource.close()
      if (systemStatsSource) systemStatsSource.close()
      if (systemLogsSource) systemLogsSource.close()
    })

    return { tasks, sources, filter, tab, pollMsg, polling, starting, batchLoading, schedulerConfig, downloadConfig, newSource, deleteTarget, editingSource,
             showAddModal, showPollModal,
             activeCount, completedCount, failedCount, waitingCount, stoppedCount, downloadingCount, mergingCount, movingCount, totalSpeed, filteredTasks, stageLabel, relativeTime,
             fetchTasks, fetchSources, fetchScheduler, fetchConfig, pollNow, pollSourceById, addSource, editSource, saveSource, toggleSource, delSource, pollSource, toggleScheduler, updateScheduler,
             deleteTask, confirmDelete,
             configSaved, configSaving, saveDownloadConfig, saveSchedulerConfig, retryTask, startTask, stopTask,
             selected, selectedCount, allSelected, canBatchStart, canBatchStop, canBatchRetry, viewMode, page, pageSize, totalPages, showPagination, goPage, setPageSize,
             toggleSelect, toggleSelectAll, batchStart, batchStop, batchRetry, batchDelete, toggleViewMode,
             addUrl, adding, addMsg, doAddVideo, canAddVideo,
             addMode, addM3u8Url, addM3u8Name, addM3u8Headers, addDownloadDir,
             proxyConfig, proxySaved, proxySaving, saveProxyConfig,
             logConfig, logSaved, logSaving, saveLogConfig,
             searchQuery, sortBy, onSearchChange, detailModal, detailTab, historyLogLines, historyLogTotal, historyLogPage, historyLogPageSize, historyLogSearch, historyLogLevel, getLogLineClass, filteredHistoryLogLines, loadHistoryLog, showDetail, closeDetail,
             systemStats, systemLogs, clearSystemLogs }
  }
}).mount('#app')
