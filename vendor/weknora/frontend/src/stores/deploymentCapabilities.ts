import { ref } from 'vue'
import { defineStore } from 'pinia'
import { getDeploymentCapabilities } from '@/api/system'
import { useAuthStore } from '@/stores/auth'
import { WANSHITONG_GATEWAY_AUTH } from '@/config/wanshitongGateway'
import {
  isDeploymentCapabilitySupported,
  type DeploymentCapabilityKey,
  type DeploymentCapabilityMap,
} from '@/config/deploymentCapabilities'

export const useDeploymentCapabilitiesStore = defineStore('deploymentCapabilities', () => {
  const edition = ref('')
  const capabilities = ref<DeploymentCapabilityMap>({})
  const loaded = ref(false)
  const loadError = ref('')
  let loadingPromise: Promise<void> | null = null

  const ensureLoaded = async (force = false): Promise<void> => {
    if (loaded.value && !force) return
    if (loadingPromise) return loadingPromise

    loadingPromise = (async () => {
      try {
        const response = await getDeploymentCapabilities()
        edition.value = response.data?.edition || ''
        capabilities.value = response.data?.capabilities || {}
        loadError.value = ''
      } catch (error) {
        // 原生模式维持既有容错；湾事通候选的关闭项仍由本地能力表禁用。
        capabilities.value = {}
        loadError.value = error instanceof Error ? error.message : String(error)
      } finally {
        loaded.value = true
        loadingPromise = null
      }
    })()

    return loadingPromise
  }

  const isSupported = (key?: DeploymentCapabilityKey) => {
    const authStore = useAuthStore()
    return isDeploymentCapabilitySupported(capabilities.value, key, {
      liteMode: authStore.isLiteMode,
      edition: edition.value,
      gatewayMode: WANSHITONG_GATEWAY_AUTH,
    })
  }

  return {
    edition,
    capabilities,
    loaded,
    loadError,
    ensureLoaded,
    isSupported,
  }
})
