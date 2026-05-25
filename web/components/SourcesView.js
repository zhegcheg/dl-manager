/**
 * 订阅源管理组件
 */
const SourcesView = {
  props: ['sources', 'newSource'],
  emits: ['addSource', 'toggleSource', 'pollSource', 'editSource', 'delSource', 'saveSource', 'update:newSource'],
  template: `
  <div>
    <div style="display:flex;align-items:center;margin-bottom:16px;">
      <h2 style="font-size:16px;color:#ff6b9d;margin:0;">📡 订阅源管理</h2>
    </div>

    <!-- 添加订阅源 -->
    <div class="add-source-form">
      <div class="row">
        <input v-model="localNewSource.name" placeholder="订阅源名称">
        <input v-model="localNewSource.url" placeholder="列表页 URL，如：https://example.com/list">
        <select v-model="localNewSource.feed_type">
          <option value="webpage">网页解析</option>
          <option value="rss">标准 RSS</option>
        </select>
      </div>
      <div class="row" style="margin-top:6px;">
        <input v-model="localNewSource.poll_cron" placeholder="定时轮询 cron，默认 0 */8 * * *（每8小时）" style="flex:2;">
        <input v-model="localNewSource.page_url_pattern" placeholder='视频链接正则，如：href="(https://example.com/videos/[^"]+)"' style="flex:3;">
        <button class="btn primary" @click="$emit('addSource')">添加</button>
      </div>
      <div style="color:#666;font-size:12px;margin-top:4px;">
        💡 必填：名称、列表页 URL、视频链接正则。其余字段（标题/m3u8/AES提取规则）可在编辑中设置，不填则使用通用规则自动识别。
      </div>
    </div>

    <!-- 订阅源列表 -->
    <div class="sources-header">
      <span style="color:#888;font-size:13px;">共 {{ sources.length }} 个订阅源</span>
    </div>
    <div class="source-list" v-if="sources.length">
      <div class="source-card" v-for="s in sources" :key="s.id">
        <div class="info">
          <div class="name">{{ s.name }} <span class="feed-type">{{ s.feed_type }}</span></div>
          <div class="url">{{ s.url }}</div>
          <div style="color:#888;font-size:12px;margin-top:2px;">⏰ {{ s.poll_cron || '0 */8 * * *' }}</div>
        </div>
        <div class="actions">
          <button class="toggle" :class="{on: s.enabled}" @click="$emit('toggleSource', s)"></button>
          <button class="btn sm" @click="$emit('pollSource', s)">🔄 轮询</button>
          <button class="btn sm" @click="$emit('editSource', s)">✏️ 编辑</button>
          <button class="btn danger sm" @click="$emit('delSource', s.id)">🗑 删除</button>
        </div>
      </div>
    </div>
    <div class="empty" v-if="!sources.length">暂无订阅源，请添加</div>
  </div>
  `,
  data() {
    return {
      localNewSource: {...this.newSource}
    }
  },
  watch: {
    'localNewSource': {
      deep: true,
      handler(val) { this.$emit('update:newSource', val) }
    },
    newSource: {
      deep: true,
      handler(val) { this.localNewSource = {...val} }
    }
  }
};
