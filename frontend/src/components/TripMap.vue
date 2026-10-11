<script setup lang="ts">
import { onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import AMapLoader from '@amap/amap-jsapi-loader'
import type { DayPlan } from '@/types'

const props = defineProps<{
  days: DayPlan[]
  activeDay: number // -1 = 显示全部天数，0/1/2… = 只显示第几天
}>()

// 每天一种颜色，标记点和路线都用这个颜色
const DAY_COLORS = ['#1677ff', '#fa541c', '#13c2c2', '#722ed1', '#52c41a', '#eb2f96', '#faad14']

const container = ref<HTMLDivElement>()
const error = ref('')
// 高德的 AMap 对象和地图实例没有 TS 类型，用 any
// shallowRef：只追踪 .value 本身的替换，不把整个地图对象变成深层响应式（否则会非常卡）
const AMap = shallowRef<any>(null)
const map = shallowRef<any>(null)

/** 根据当前数据重新绘制标记点和路线 */
function draw() {
  if (!map.value || !AMap.value) return
  map.value.clearMap() // 清掉上一次画的覆盖物

  props.days.forEach((day, dayIndex) => {
    if (props.activeDay !== -1 && props.activeDay !== dayIndex) return
    const color = DAY_COLORS[dayIndex % DAY_COLORS.length]
    // 高德要求坐标格式是 [经度, 纬度]
    const path = day.attractions.map((a) => [a.location.longitude, a.location.latitude])

    day.attractions.forEach((attraction, i) => {
      const marker = new AMap.value.Marker({
        position: path[i],
        title: attraction.name,
        // 用 HTML 自定义标记的样式：圆点里显示"天数-序号"
        content: `<div style="background:${color};color:#fff;border-radius:12px;padding:2px 8px;font-size:12px;white-space:nowrap">${dayIndex + 1}-${i + 1} ${attraction.name}</div>`,
        offset: new AMap.value.Pixel(-12, -12),
      })
      map.value.add(marker)
    })

    // 至少两个点才能连成线
    if (path.length > 1) {
      map.value.add(new AMap.value.Polyline({ path, strokeColor: color, strokeWeight: 4, strokeOpacity: 0.8 }))
    }
  })

  map.value.setFitView() // 自动缩放，让所有覆盖物都能显示在视野里
}

onMounted(async () => {
  const key = import.meta.env.VITE_AMAP_JS_KEY
  if (!key) {
    error.value = '未配置 VITE_AMAP_JS_KEY，地图不可用（其他功能不受影响）'
    return
  }
  // 安全密钥必须在加载 JS API 之前设置好
  if (import.meta.env.VITE_AMAP_SECURITY_CODE) {
    window._AMapSecurityConfig = { securityJsCode: import.meta.env.VITE_AMAP_SECURITY_CODE }
  }
  try {
    // 动态加载高德 JS API（相当于动态插入一个 <script src="https://webapi.amap.com/maps?...">）
    AMap.value = await AMapLoader.load({ key, version: '2.0' })
    map.value = new AMap.value.Map(container.value, { zoom: 12, viewMode: '2D' })
    draw()
  } catch (e) {
    error.value = `地图加载失败: ${String(e)}`
  }
})

// 编辑行程、切换天数时重新绘制；deep: true 才能监听到数组内部的变化（比如调整了景点顺序）
watch(() => [props.days, props.activeDay], draw, { deep: true })

// 组件卸载时销毁地图，释放 WebGL 资源，否则反复进出页面会内存泄漏
onUnmounted(() => map.value?.destroy())
</script>

<template>
  <a-alert v-if="error" :message="error" type="warning" show-icon />
  <div v-else ref="container" class="trip-map" />
</template>

<style scoped>
.trip-map {
  width: 100%;
  height: 420px;
  border-radius: 8px;
}
</style>
