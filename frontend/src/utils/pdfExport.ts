import html2canvas from 'html2canvas'
import jsPDF from 'jspdf'

export type PdfExportOptions = {
  filename?: string
  margin?: number
  scale?: number
  showPageNumbers?: boolean
}

const DEFAULT_OPTIONS: Required<PdfExportOptions> = {
  filename: 'report',
  margin: 16,
  scale: 2,
  showPageNumbers: true,
}

async function convertSvgsToImages(element: HTMLElement) {
  const svgs = Array.from(element.querySelectorAll('svg'))
  if (!svgs.length) return

  await Promise.all(
    svgs.map(async (svg) => {
      try {
        const bbox = svg.getBoundingClientRect()
        const width = Math.max(bbox.width, 120)
        const height = Math.max(bbox.height, 120)
        const clonedSvg = svg.cloneNode(true) as SVGElement
        clonedSvg.setAttribute('width', String(width))
        clonedSvg.setAttribute('height', String(height))
        if (!clonedSvg.getAttribute('xmlns')) {
          clonedSvg.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
        }

        const svgString = new XMLSerializer().serializeToString(clonedSvg)
        const blob = new Blob([svgString], { type: 'image/svg+xml;charset=utf-8' })
        const url = URL.createObjectURL(blob)

        const image = new Image()
        await new Promise<void>((resolve) => {
          image.onload = () => resolve()
          image.onerror = () => resolve()
          image.src = url
        })

        if (image.width && image.height) {
          const canvas = document.createElement('canvas')
          const scale = 2
          canvas.width = width * scale
          canvas.height = height * scale
          const ctx = canvas.getContext('2d')
          if (ctx) {
            ctx.fillStyle = '#fff'
            ctx.fillRect(0, 0, canvas.width, canvas.height)
            ctx.scale(scale, scale)
            ctx.drawImage(image, 0, 0, width, height)

            const replacement = document.createElement('img')
            replacement.src = canvas.toDataURL('image/png')
            replacement.style.width = `${width}px`
            replacement.style.height = `${height}px`
            replacement.style.display = 'block'
            replacement.style.margin = '12px auto'
            svg.parentNode?.replaceChild(replacement, svg)
          }
        }

        URL.revokeObjectURL(url)
      } catch (error) {
        console.warn('PDF export svg conversion failed:', error)
      }
    }),
  )
}

function splitCanvasIntoPages(
  canvas: HTMLCanvasElement,
  pageWidth: number,
  pageHeight: number,
  margin: number,
) {
  const contentWidth = pageWidth - margin * 2
  const contentHeight = pageHeight - margin * 2
  const pageHeightInPixels = Math.max(1, Math.floor((canvas.width * contentHeight) / contentWidth))
  const pageCount = Math.ceil(canvas.height / pageHeightInPixels)
  const pages: HTMLCanvasElement[] = []

  for (let i = 0; i < pageCount; i += 1) {
    const pageCanvas = document.createElement('canvas')
    pageCanvas.width = canvas.width
    pageCanvas.height = Math.min(pageHeightInPixels, canvas.height - i * pageHeightInPixels)
    const ctx = pageCanvas.getContext('2d')
    if (ctx) {
      ctx.fillStyle = '#fff'
      ctx.fillRect(0, 0, pageCanvas.width, pageCanvas.height)
      ctx.drawImage(
        canvas,
        0,
        i * pageHeightInPixels,
        canvas.width,
        pageCanvas.height,
        0,
        0,
        canvas.width,
        pageCanvas.height,
      )
    }
    pages.push(pageCanvas)
  }

  return { pages, pageCount }
}

export async function exportToPdf(element: HTMLElement, options: PdfExportOptions = {}) {
  const opts = { ...DEFAULT_OPTIONS, ...options }
  const width = Math.max(element.getBoundingClientRect().width, 800)
  const clone = element.cloneNode(true) as HTMLElement
  clone.style.position = 'fixed'
  clone.style.top = '-99999px'
  clone.style.left = '0'
  clone.style.width = `${width}px`
  clone.style.background = '#fff'
  document.body.appendChild(clone)

  try {
    await new Promise((resolve) => setTimeout(resolve, 200))
    await convertSvgsToImages(clone)
    await new Promise((resolve) => setTimeout(resolve, 200))

    const canvas = await html2canvas(clone, {
      scale: opts.scale,
      useCORS: true,
      backgroundColor: '#ffffff',
      logging: false,
      windowWidth: width,
      allowTaint: true,
    })

    const pdf = new jsPDF('p', 'mm', 'a4')
    const pageWidth = pdf.internal.pageSize.getWidth()
    const pageHeight = pdf.internal.pageSize.getHeight()
    const contentWidth = pageWidth - opts.margin * 2
    const { pages, pageCount } = splitCanvasIntoPages(canvas, pageWidth, pageHeight, opts.margin)

    pages.forEach((pageCanvas, index) => {
      if (index > 0) pdf.addPage()
      const imgHeight = (pageCanvas.height * contentWidth) / pageCanvas.width
      const imgData = pageCanvas.toDataURL('image/png')
      pdf.addImage(imgData, 'PNG', opts.margin, opts.margin, contentWidth, imgHeight)

      if (opts.showPageNumbers) {
        pdf.setFontSize(10)
        pdf.setTextColor(120)
        pdf.text(`${index + 1} / ${pageCount}`, pageWidth - opts.margin, pageHeight - 6, {
          align: 'right',
        })
      }
    })

    pdf.save(`${opts.filename}.pdf`)
  } finally {
    document.body.removeChild(clone)
  }
}
