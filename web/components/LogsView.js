/**
 * 系统日志组件
 */
const LogsView = {
  props: ['systemLogs'],
  emits: ['clearLogs'],
  data() {
    return {
      filterLevel: 'all',  // all, info, warn, error
      searchQuery: ''
    }
  },
  computed: {
    filteredLogs() {
      let logs = this.systemLogs
      
      // 按级别筛选
      if (this.filterLevel !== 'all') {
        logs = logs.filter(line => {
          if (this.filterLevel === 'error') return line.includes('ERROR') || line.includes('失败')
          if (this.filterLevel === 'warn') return line.includes('WARN') || line.includes('警告')
          if (this.filterLevel === 'info') return line.includes('INFO')
          if (this.filterLevel === 'debug') return line.includes('DEBUG')
          return true
        })
      }
      
      // 按关键词搜索
      if (this.searchQuery) {
        const query = this.searchQuery.toLowerCase()
        logs = logs.filter(line => line.toLowerCase().includes(query))
      }
      
      return logs
    },
    levelCounts() {
      const counts = { all: this.systemLogs.length, error: 0, warn: 0, info: 0, debug: 0 }
      this.systemLogs.forEach(line => {
        if (line.includes('ERROR') || line.includes('失败')) counts.error++
        else if (line.includes('WARN') || line.includes('警告')) counts.warn++
        else if (line.includes('INFO')) counts.info++
        else if (line.includes('DEBUG')) counts.debug++
      })
      return counts
    }
  },
  methods: {
    getLineClass(line) {
      if (line.includes('ERROR') || line.includes('失败')) return 'log-error'
      if (line.includes('WARN') || line.includes('警告')) return 'log-warn'
      if (line.includes('INFO')) return 'log-info'
      if (line.includes('DEBUG')) return 'log-debug'
      return 'log-default'
    }
  },
  template: `
  <div>
    <div style="display:flex;align-items:center;margin-bottom:16px;">
      <h2 style="font-size:16px;color:#ff6b9d;margin:0;">📜 系统日志</h2>
      <span style="margin-left:12px;font-size:12px;color:#888;">应用运行日志</span>
      <span style="margin-left:auto;font-size:12px;color:#888;">共 {{ levelCounts.all }} 条</span>
      <button class="btn sm" @click="$emit('clearLogs')" style="margin-left:8px;border-color:#888;color:#888;" v-if="systemLogs.length">清空</button>
    </div>
    
    <!-- 筛选栏 -->
    <div style="display:flex;gap:12px;margin-bottom:12px;align-items:center;flex-wrap:wrap;">
      <div style="display:flex;gap:4px;">
        <button class="btn sm" :style="filterLevel==='all' ? 'background:#ff6b9d;color:#fff;border-color:#ff6b9d;' : 'border-color:#333;color:#888;'" @click="filterLevel='all'">全部 ({{ levelCounts.all }})</button>
        <button class="btn sm" :style="filterLevel==='error' ? 'background:#f87171;color:#fff;border-color:#f87171;' : 'border-color:#333;color:#888;'" @click="filterLevel='error'">ERROR ({{ levelCounts.error }})</button>
        <button class="btn sm" :style="filterLevel==='warn' ? 'background:#fbbf24;color:#fff;border-color:#fbbf24;' : 'border-color:#333;color:#888;'" @click="filterLevel='warn'">WARN ({{ levelCounts.warn }})</button>
        <button class="btn sm" :style="filterLevel==='info' ? 'background:#4ade80;color:#fff;border-color:#4ade80;' : 'border-color:#333;color:#888;'" @click="filterLevel='info'">INFO ({{ levelCounts.info }})</button>
        <button class="btn sm" :style="filterLevel==='debug' ? 'background:#60a5fa;color:#fff;border-color:#60a5fa;' : 'border-color:#333;color:#888;'" @click="filterLevel='debug'">DEBUG ({{ levelCounts.debug }})</button>
      </div>
      <input v-model="searchQuery" placeholder="🔍 搜索日志..." style="flex:1;min-width:200px;padding:6px 12px;border-radius:6px;border:1px solid #333;background:#1a1a1a;color:#e0e0e0;font-size:13px;">
      <button class="btn sm" v-if="searchQuery" @click="searchQuery=''" style="border-color:#888;color:#888;">清除搜索</button>
    </div>
    
    <!-- 日志显示 -->
    <div class="log-container" style="max-height:60vh;overflow-y:auto;font-family:'Courier New',monospace;font-size:13px;line-height:1.6;padding:16px;background:#111;border-radius:8px;border:1px solid #333;">
      <div v-if="filteredLogs.length === 0" style="color:#666;text-align:center;padding:40px;">
        <div v-if="searchQuery">未找到匹配的日志</div>
        <div v-else>暂无日志</div>
      </div>
      <div v-for="(line, idx) in filteredLogs" :key="idx" style="padding:2px 0;" :class="getLineClass(line)">{{ line }}</div>
    </div>
  </div>
  `
};
