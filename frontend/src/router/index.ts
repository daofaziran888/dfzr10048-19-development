import { createRouter, createWebHistory } from 'vue-router'

import FailRedeliverClaimView from '@/views/FailRedeliverClaimView.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 固定路径必须放在 /:moduleKey 之前，否则会被模块页抢先匹配。
    { path: '/fail_redeliver/claim', name: 'fail-redeliver-claim', component: FailRedeliverClaimView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
