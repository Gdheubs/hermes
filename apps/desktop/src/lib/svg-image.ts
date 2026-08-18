// Rasterise an SVG string to PNG and copy it to the clipboard. Self-contained
// SVGs only (inline styles) — mermaid output qualifies. Falls back to copying
// the SVG markup as text where image clipboard writes aren't permitted.

// Mermaid (and other generators) emit `width="100%"` for responsive diagrams.
// A percentage is not a usable intrinsic size: inside the zoom viewer's
// shrink-to-fit grid item it can collapse the diagram, and `drawImage` treats
// parseFloat("100%") as 100 blob pixels (a tiny/broken PNG). Where the root
// svg's width/height are percentages, substitute the pixel size implied by the
// viewBox so the diagram keeps a real intrinsic size. Returns the ORIGINAL
// string when no substitution is needed (no 100% width, or no usable viewBox).
export function normalizeSvgSize(svg: string): string {
  const el = new DOMParser().parseFromString(svg, 'image/svg+xml').documentElement

  if (el.tagName !== 'svg') {
    return svg
  }

  const width = el.getAttribute('width')
  const height = el.getAttribute('height')

  if (width !== '100%' && !(width ?? '').trim().endsWith('%')) {
    return svg
  }

  const [, , vbW, vbH] = (el.getAttribute('viewBox') || '').split(/[\s,]+/).map(Number)

  if (!(vbW > 0) || !(vbH > 0)) {
    return svg
  }

  el.setAttribute('width', String(vbW))
  el.setAttribute('height', String(vbH))

  return new XMLSerializer().serializeToString(el)
}

function parseSvgLength(raw: string | null): number | null {
  if (!raw) {
    return null
  }

  const value = Number.parseFloat(raw)

  // A percentage (e.g. mermaid's width="100%") is not a pixel size — treat it
  // as absent and fall back to the viewBox.
  if (!Number.isFinite(value) || raw.trim().endsWith('%')) {
    return null
  }

  return value
}

export function svgSize(svg: string): { height: number; width: number } {
  const el = new DOMParser().parseFromString(svg, 'image/svg+xml').documentElement
  const width = parseSvgLength(el.getAttribute('width'))
  const height = parseSvgLength(el.getAttribute('height'))

  if (width && height) {
    return { height, width }
  }

  const [, , vbW, vbH] = (el.getAttribute('viewBox') || '').split(/[\s,]+/).map(Number)

  return vbW && vbH ? { height: vbH, width: vbW } : { height: 600, width: 800 }
}

export function svgToPngBlob(svg: string, scale = 2): Promise<Blob> {
  const { height, width } = svgSize(svg)

  return new Promise((resolve, reject) => {
    const image = new Image()

    image.onload = () => {
      const canvas = document.createElement('canvas')
      canvas.width = Math.max(1, Math.round(width * scale))
      canvas.height = Math.max(1, Math.round(height * scale))

      const ctx = canvas.getContext('2d')

      if (!ctx) {
        reject(new Error('no 2d context'))

        return
      }

      ctx.scale(scale, scale)
      ctx.drawImage(image, 0, 0, width, height)
      canvas.toBlob(blob => (blob ? resolve(blob) : reject(new Error('toBlob failed'))), 'image/png')
    }

    image.onerror = () => reject(new Error('svg load failed'))
    image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
  })
}

export async function copySvgAsPng(svg: string): Promise<void> {
  try {
    const blob = await svgToPngBlob(svg)

    await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })])
  } catch {
    await navigator.clipboard.writeText(svg)
  }
}
