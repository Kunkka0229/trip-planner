import html2canvas from 'html2canvas'
import { jsPDF } from 'jspdf'

/** 把一个 DOM 元素"截图"成 canvas（html2canvas 会把 DOM 和 CSS 重新绘制到 canvas 上，并不是真正的截屏） */
async function renderToCanvas(element: HTMLElement): Promise<HTMLCanvasElement> {
  return html2canvas(element, {
    scale: 2, // 2 倍分辨率，导出的图片在高清屏上不会模糊
    useCORS: true, // 允许加载跨域图片（需要图片服务器也允许跨域）
    backgroundColor: '#f6f8fb',
    // 忽略带 data-export-ignore 属性的元素（比如编辑按钮、导出按钮本身）
    ignoreElements: (el) => el.hasAttribute('data-export-ignore'),
  })
}

/** 导出为 PNG 图片 */
export async function exportAsImage(element: HTMLElement, filename: string): Promise<void> {
  const canvas = await renderToCanvas(element)
  // 创建一个临时的 <a download> 并模拟点击，触发浏览器下载
  const link = document.createElement('a')
  link.download = `${filename}.png`
  link.href = canvas.toDataURL('image/png')
  link.click()
}

/** 导出为 PDF：把长图按 A4 页高切成多页 */
export async function exportAsPdf(element: HTMLElement, filename: string): Promise<void> {
  const canvas = await renderToCanvas(element)
  const pdf = new jsPDF({ orientation: 'portrait', unit: 'mm', format: 'a4' })

  const pageWidth = pdf.internal.pageSize.getWidth() // 210mm
  const pageHeight = pdf.internal.pageSize.getHeight() // 297mm
  // 图片宽度撑满页面，按比例算出整张长图在 PDF 里的高度
  const imgHeight = (canvas.height * pageWidth) / canvas.width
  const imgData = canvas.toDataURL('image/jpeg', 0.92) // JPEG 比 PNG 小很多

  // 同一张长图贴到每一页，每页向上偏移一个页高，相当于"分页滚动"
  let offset = 0
  while (offset < imgHeight) {
    if (offset > 0) pdf.addPage()
    pdf.addImage(imgData, 'JPEG', 0, -offset, pageWidth, imgHeight)
    offset += pageHeight
  }
  pdf.save(`${filename}.pdf`)
}
