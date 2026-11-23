export const stubsViewTemplate = `
<section class="panel">
  <h2>空桩台</h2>
  <p class="hint">承运商导入改批走单轨门闸：过闸只落影子意图待审核，正式批零改；未过闸拒写且桩记录零增。</p>
  <p class="hint">
    门闸纪要 · 版本 {{ carrierGate.memo.version || "—" }} ·
    <span class="badge" :class="carrierGate.memo.sealed ? 'pass' : 'fail'">{{ carrierGate.memo.status || "未封死" }}</span>
    · 待审核意图 {{ carrierGate.pending_intents || 0 }}
  </p>
  <div class="grid-2">
    <div>
      <h3>承运商导入空桩</h3>
      <div class="form-row">
        <div><label>承运商</label><input v-model="carrierForm.carrier_name"/></div>
        <div><label>行数</label><input type="number" v-model.number="carrierForm.row_count"/></div>
      </div>
      <button @click="doCarrierStub">登记空桩</button>
      <ul><li v-for="j in carrierJobs" :key="j.id">{{ j.carrier_name }} {{ j.row_count }}行 · {{ j.status }}</li></ul>
    </div>
    <div>
      <h3>解冻事件空桩</h3>
      <div class="form-row">
        <div><label>批次ID(可空)</label><input type="number" v-model.number="thawForm.batch_id"/></div>
        <div><label>分钟</label><input type="number" v-model.number="thawForm.thaw_minutes"/></div>
      </div>
      <button @click="doThawStub">登记解冻</button>
      <ul><li v-for="j in thawJobs" :key="j.id">批次 {{ j.batch_id }} {{ j.thaw_minutes }}分 · {{ j.status }}</li></ul>
    </div>
  </div>
  <h3>承运商改批 · 影子导入意图</h3>
  <div class="form-row">
    <div><label>承运商</label><input v-model="carrierChangeForm.carrier_name"/></div>
    <div><label>批次ID</label><input type="number" v-model.number="carrierChangeForm.batch_id"/></div>
    <div><label>回写行数</label><input type="number" v-model.number="carrierChangeForm.row_count"/></div>
    <div><label>提议货载编号</label><input v-model="carrierChangeForm.cargo_ref"/></div>
  </div>
  <label>提议温度点 JSON(可空)</label>
  <textarea v-model="carrierChangeForm.points_json" rows="3" placeholder="[{&quot;t&quot;:&quot;08:00&quot;,&quot;c&quot;:-19}]"></textarea>
  <div class="actions">
    <button @click="doCarrierChange">提交影子意图</button>
    <button class="secondary" @click="refresh">刷新</button>
  </div>
  <table class="data-table">
    <thead><tr><th>意图</th><th>承运商</th><th>批次</th><th>状态</th><th>审核时间</th><th></th></tr></thead>
    <tbody>
      <tr v-for="i in carrierIntents" :key="i.id">
        <td>#{{ i.id }}</td>
        <td>{{ i.carrier_name }}</td>
        <td>{{ i.batch_code }}</td>
        <td><span class="badge">{{ i.status }}</span></td>
        <td>{{ i.reviewed_at || "—" }}</td>
        <td>
          <button v-if="i.status==='待审核'" class="mini-btn" @click="reviewCarrierIntent(i.id, true)">审核通过</button>
          <button v-if="i.status==='待审核'" class="mini-btn" @click="reviewCarrierIntent(i.id, false)">审核拒收</button>
        </td>
      </tr>
    </tbody>
  </table>
</section>
`;
