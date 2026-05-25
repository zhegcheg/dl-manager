/**
 * 设置页面组件
 */
const SettingsView = {
  props: ['downloadConfig', 'proxyConfig', 'logConfig', 'configSaving', 'configSaved', 'proxySaving', 'proxySaved', 'logSaving', 'logSaved'],
  emits: ['saveDownloadConfig', 'saveProxyConfig', 'saveLogConfig'],
  template: `
  <div class="settings-container">
    <!-- 页面标题 -->
    <div class="settings-header">
      <h2>⚙️ 系统设置</h2>
      <p class="settings-desc">配置下载、代理和日志相关参数</p>
    </div>

    <!-- 设置卡片网格 -->
    <div class="settings-grid">
      <!-- 下载配置 -->
      <div class="settings-card">
        <div class="card-header">
          <h3>💾 下载设置</h3>
        </div>
        <div class="card-body">
          <div class="form-group">
            <label class="form-label">下载目录</label>
            <input v-model="downloadConfig.download_dir" placeholder="/home/zhegcheg/imovie/tasks" class="form-input">
          </div>
          <div class="form-group">
            <label class="form-label">
              临时目录
              <span class="form-hint">分片下载后合并，再移动到下载目录</span>
            </label>
            <input v-model="downloadConfig.temp_dir" placeholder="/home/zhegcheg/imovie/temp" class="form-input">
          </div>
          <div class="form-row">
            <div class="form-group">
              <label class="form-label">
                最大并发数
                <span class="form-hint">(上限10)</span>
              </label>
              <input v-model="downloadConfig.max_concurrent" placeholder="2" type="number" min="1" max="10" class="form-input form-input-sm">
            </div>
            <div class="form-group">
              <label class="form-label">
                每任务线程数
                <span class="form-hint">(上限16)</span>
              </label>
              <input v-model="downloadConfig.thread_count" placeholder="8" type="number" min="1" max="16" class="form-input form-input-sm">
            </div>
          </div>
          <div class="form-group">
            <label class="form-label">
              NAS 转移
              <span class="form-hint">下载完成后自动复制到 NAS</span>
            </label>
            <div class="toggle-row">
              <span class="toggle-path">/mnt/fn-nas-imovie/</span>
              <button class="toggle" :class="{on: downloadConfig.move_to_nas==='true'}" @click="downloadConfig.move_to_nas = downloadConfig.move_to_nas==='true' ? 'false' : 'true'"></button>
              <span class="toggle-status" :style="{color: downloadConfig.move_to_nas==='true' ? '#4ade80' : '#888'}">{{ downloadConfig.move_to_nas==='true' ? '已启用' : '已关闭' }}</span>
            </div>
          </div>
        </div>
        <div class="card-footer">
          <button class="btn btn-primary" @click="$emit('saveDownloadConfig')" :disabled="configSaving">
            💾 {{ configSaving ? '保存中...' : '保存设置' }}
          </button>
          <span class="success-msg" v-if="configSaved">✅ {{ configSaved }}</span>
        </div>
      </div>

      <!-- 代理设置 -->
      <div class="settings-card">
        <div class="card-header">
          <h3>🌐 代理设置</h3>
        </div>
        <div class="card-body">
          <div class="form-group">
            <label class="form-label">启用代理</label>
            <div class="toggle-row">
              <button class="toggle" :class="{on: proxyConfig.enabled==='true'}" @click="proxyConfig.enabled = proxyConfig.enabled==='true' ? 'false' : 'true'"></button>
              <span class="toggle-status" :style="{color: proxyConfig.enabled==='true' ? '#4ade80' : '#888'}">{{ proxyConfig.enabled==='true' ? '已启用' : '已关闭' }}</span>
            </div>
          </div>
          <div class="form-group">
            <label class="form-label">代理类型</label>
            <select v-model="proxyConfig.type" class="form-select">
              <option value="http">HTTP</option>
              <option value="socks5">SOCKS5</option>
            </select>
          </div>
          <div class="form-row">
            <div class="form-group">
              <label class="form-label">主机地址</label>
              <input v-model="proxyConfig.host" placeholder="127.0.0.1" class="form-input">
            </div>
            <div class="form-group">
              <label class="form-label">端口号</label>
              <input v-model="proxyConfig.port" placeholder="7890" type="number" min="1" max="65535" class="form-input">
            </div>
          </div>
          <div class="form-row">
            <div class="form-group">
              <label class="form-label">
                用户名
                <span class="form-hint">可选</span>
              </label>
              <input v-model="proxyConfig.username" placeholder="" class="form-input">
            </div>
            <div class="form-group">
              <label class="form-label">
                密码
                <span class="form-hint">可选</span>
              </label>
              <input v-model="proxyConfig.password" placeholder="" type="password" class="form-input">
            </div>
          </div>
        </div>
        <div class="card-footer">
          <button class="btn btn-primary" @click="$emit('saveProxyConfig')" :disabled="proxySaving">
            💾 {{ proxySaving ? '保存中...' : '保存代理' }}
          </button>
          <span class="success-msg" v-if="proxySaved">✅ {{ proxySaved }}</span>
        </div>
      </div>

      <!-- 日志配置 -->
      <div class="settings-card full-width">
        <div class="card-header">
          <h3>📝 日志配置</h3>
        </div>
        <div class="card-body">
          <div class="form-row">
            <div class="form-group">
              <label class="form-label">
                日志级别
                <span class="form-hint">DEBUG/INFO/WARNING/ERROR</span>
              </label>
              <select v-model="logConfig.log_level" class="form-select">
                <option value="DEBUG">DEBUG（详细）</option>
                <option value="INFO">INFO（默认）</option>
                <option value="WARNING">WARNING（警告）</option>
                <option value="ERROR">ERROR（仅错误）</option>
              </select>
            </div>
            <div class="form-group" style="flex: 2;">
              <label class="form-label">
                日志保存路径
                <span class="form-hint">支持绝对路径，留空使用默认路径</span>
              </label>
              <input v-model="logConfig.log_path" placeholder="/home/zhegcheg/.dl-manager/logs/dl-manager.log" class="form-input">
            </div>
          </div>
        </div>
        <div class="card-footer">
          <button class="btn btn-primary" @click="$emit('saveLogConfig')" :disabled="logSaving">
            💾 {{ logSaving ? '保存中...' : '保存日志配置' }}
          </button>
          <span class="success-msg" v-if="logSaved">✅ {{ logSaved }}</span>
        </div>
      </div>
    </div>
  </div>
  `
};
