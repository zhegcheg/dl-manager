/**
 * 订阅源管理组件
 */
const SourcesView = {
  props: ['sources', 'newSource', 'editingSource'],
  emits: ['addSource', 'toggleSource', 'pollSource', 'editSource', 'delSource', 'saveSource', 'update:newSource', 'update:editingSource'],
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

    <!-- 编辑订阅源弹窗 -->
    <div class="overlay" v-if="localEditingSource" @click.self="localEditingSource=null">
      <div class="panel" style="max-width:640px;">
        <div class="panel-header">
          <h2>编辑订阅源</h2>
          <button class="panel-close" @click="localEditingSource=null">×</button>
        </div>
        <div class="panel-body" style="max-height:70vh;overflow-y:auto;">
          <div class="form-row">
            <label>名称</label>
            <input v-model="localEditingSource.name" placeholder="订阅源名称">
          </div>
          <div class="form-row">
            <label>列表页 URL</label>
            <input v-model="localEditingSource.url" placeholder="列表页 URL">
          </div>
          <div class="form-row">
            <label>类型</label>
            <select v-model="localEditingSource.feed_type">
              <option value="webpage">网页解析</option>
              <option value="rss">标准 RSS</option>
            </select>
          </div>
          <div class="form-row">
            <label>定时轮询 (cron)</label>
            <input v-model="localEditingSource.poll_cron" placeholder="0 */8 * * *">
            <div style="color:#888;font-size:12px;">格式：分 时 日 月 周。如 <code>0 */8 * * *</code> = 每8小时，<code>0 4 * * *</code> = 每天4点</div>
          </div>
          <hr style="border-color:#333;margin:12px 0;">
          <div style="color:#ff6b9d;font-size:13px;margin-bottom:8px;">🔧 解析规则（不填则使用通用规则自动识别）</div>
          <div class="form-row">
            <label>视频链接正则</label>
            <input v-model="localEditingSource.page_url_pattern" placeholder='从列表页提取视频链接，如：href="(https://example.com/videos/[^"]+)"'>
            <div style="color:#888;font-size:12px;">必须包含一个捕获组 (...)，匹配视频页 URL</div>
          </div>
          <div class="form-row">
            <label>标题正则</label>
            <input v-model="localEditingSource.title_selector" placeholder="从视频页提取标题，默认 &lt;title&gt; 标签">
          </div>
          <div class="form-row">
            <label>m3u8 正则</label>
            <input v-model="localEditingSource.m3u8_selector" placeholder="从页面提取 m3u8 URL 的正则，不填则自动查找">
          </div>
          <div class="form-row">
            <label>视频 ID 正则</label>
            <input v-model="localEditingSource.video_id_pattern" placeholder="从视频 URL 提取 ID，不填则取 URL 最后一段">
          </div>
          <div class="form-row">
            <label>Referer</label>
            <input v-model="localEditingSource.referer" placeholder="请求头 Referer，如：https://example.com/">
          </div>
          <div class="form-row">
            <label>自定义 Headers</label>
            <textarea v-model="localEditingSource.headers" rows="2" placeholder="每行一个，如：&#10;Cookie: xxx=yyy&#10;X-Token: abc"></textarea>
          </div>
          <div class="form-row">
            <label>AES Key 正则</label>
            <input v-model="localEditingSource.key_selector" placeholder="从页面提取 AES 密钥的正则">
          </div>
          <div class="form-row">
            <label>AES IV 正则</label>
            <input v-model="localEditingSource.iv_selector" placeholder="从页面提取 AES IV 的正则">
          </div>
          <div class="form-row">
            <label>刷新 URL 模板</label>
            <input v-model="localEditingSource.refresh_url_pattern" placeholder="下载前刷新 m3u8 的 URL，用 {task_id} 作占位符">
            <div style="color:#888;font-size:12px;">如：<code>https://example.com/videos/{task_id}/</code></div>
          </div>
        </div>
        <div class="panel-footer">
          <button class="btn" @click="localEditingSource=null">取消</button>
          <button class="btn primary" @click="$emit('saveSource')">保存</button>
        </div>
      </div>
    </div>
  </div>
  `,
  data() {
    return {
      localNewSource: {...this.newSource},
      localEditingSource: this.editingSource ? {...this.editingSource} : null
    }
  },
  watch: {
    'localNewSource': {
      deep: true,
      handler(val) { this.$emit('update:newSource', val) }
    },
    'localEditingSource': {
      deep: true,
      handler(val) { this.$emit('update:editingSource', val) }
    },
    newSource: {
      deep: true,
      handler(val) { this.localNewSource = {...val} }
    },
    editingSource(val) {
      this.localEditingSource = val ? {...val} : null
    }
  }
};
