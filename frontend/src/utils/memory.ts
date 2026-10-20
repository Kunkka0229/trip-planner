// P7：记忆分类的中文名和显示顺序，和后端 memories.py 里 NewMemory.category 的取值一一对应
// 后端新增了分类，这里没跟上时会归到"其他"，不会显示不出来
export const MEMORY_CATEGORIES = [
  { key: 'party', label: '同行人员', icon: '👨‍👩‍👧' },
  { key: 'diet', label: '饮食', icon: '🍜' },
  { key: 'pace', label: '节奏', icon: '🚶' },
  { key: 'lodging', label: '住宿', icon: '🏨' },
  { key: 'interest', label: '兴趣', icon: '🏛️' },
  { key: 'budget', label: '预算', icon: '💰' },
  { key: 'other', label: '其他', icon: '📝' },
] as const

const KNOWN = new Set<string>(MEMORY_CATEGORIES.map((c) => c.key))

/** 不认识的分类当成 other */
export function normalizeCategory(category: string): string {
  return KNOWN.has(category) ? category : 'other'
}
