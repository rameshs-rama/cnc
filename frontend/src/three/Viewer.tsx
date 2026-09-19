/**
 * 3D viewport.
 *
 * The part surface is drawn from the same height field the verification engine
 * compares against, so what the engineer sees is what the simulation checked -
 * not a separate cosmetic model that could drift from it.
 */

import { useEffect, useRef } from 'react'
import * as THREE from 'three'
import type { Move, Surface } from '@/lib/api'
import { STATUS_COLOR } from '@/lib/format'

export interface FeatureMarker {
  key: string
  label: string
  position: [number, number, number]
  status: string
  radius: number
  selected?: boolean
}

export interface ViewerProps {
  surface?: Surface | null
  /** Remaining stock after simulation, on the same grid. */
  stock?: { grid: Surface['grid']; height: number[][] } | null
  toolpaths?: Array<{ id: string; label: string; moves: Move[]; visible: boolean; color?: string }>
  markers?: FeatureMarker[]
  onSelect?: (key: string | null) => void
  showStock?: boolean
  showSurface?: boolean
  height?: number
}

const RAPID_COLOR = new THREE.Color('#f85149')
const FEED_COLOR = new THREE.Color('#58a6ff')
const PLUNGE_COLOR = new THREE.Color('#d29922')

export function Viewer({
  surface,
  stock,
  toolpaths = [],
  markers = [],
  onSelect,
  showStock = true,
  showSurface = true,
  height = 460,
}: ViewerProps) {
  const mountRef = useRef<HTMLDivElement>(null)
  const stateRef = useRef<{
    renderer: THREE.WebGLRenderer
    scene: THREE.Scene
    camera: THREE.PerspectiveCamera
    content: THREE.Group
    markerGroup: THREE.Group
    raycaster: THREE.Raycaster
    dispose: () => void
  } | null>(null)

  // --- one-time scene setup -------------------------------------------------
  useEffect(() => {
    const mount = mountRef.current
    if (!mount) return

    const scene = new THREE.Scene()
    scene.background = new THREE.Color('#0d1117')

    const camera = new THREE.PerspectiveCamera(45, mount.clientWidth / height, 0.5, 5000)
    camera.position.set(180, -180, 150)
    camera.up.set(0, 0, 1)

    const renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.setSize(mount.clientWidth, height)
    mount.appendChild(renderer.domElement)

    scene.add(new THREE.AmbientLight(0xffffff, 0.55))
    const key = new THREE.DirectionalLight(0xffffff, 1.0)
    key.position.set(120, -160, 240)
    scene.add(key)
    const fill = new THREE.DirectionalLight(0x88aaff, 0.35)
    fill.position.set(-200, 120, 80)
    scene.add(fill)

    const grid = new THREE.GridHelper(400, 40, 0x30363d, 0x21262d)
    grid.rotation.x = Math.PI / 2
    scene.add(grid)
    scene.add(new THREE.AxesHelper(40))

    const content = new THREE.Group()
    scene.add(content)
    const markerGroup = new THREE.Group()
    scene.add(markerGroup)

    // Orbit and zoom without pulling in a control dependency.
    let dragging = false
    let lastX = 0
    let lastY = 0
    const spherical = new THREE.Spherical(320, Math.PI / 3, Math.PI / 4)
    const target = new THREE.Vector3(0, 0, 0)

    const applyCamera = () => {
      const offset = new THREE.Vector3().setFromSpherical(spherical)
      camera.position.copy(target).add(new THREE.Vector3(offset.x, offset.z, offset.y))
      camera.up.set(0, 0, 1)
      camera.lookAt(target)
    }
    applyCamera()

    const onPointerDown = (event: PointerEvent) => {
      dragging = true
      lastX = event.clientX
      lastY = event.clientY
      ;(event.target as Element).setPointerCapture?.(event.pointerId)
    }
    const onPointerMove = (event: PointerEvent) => {
      if (!dragging) return
      const dx = event.clientX - lastX
      const dy = event.clientY - lastY
      lastX = event.clientX
      lastY = event.clientY
      spherical.theta -= dx * 0.006
      spherical.phi = Math.max(0.05, Math.min(Math.PI - 0.05, spherical.phi - dy * 0.006))
      applyCamera()
    }
    const onPointerUp = () => {
      dragging = false
    }
    const onWheel = (event: WheelEvent) => {
      event.preventDefault()
      spherical.radius = Math.max(30, Math.min(2000, spherical.radius * (1 + event.deltaY * 0.0012)))
      applyCamera()
    }

    const raycaster = new THREE.Raycaster()
    const onClick = (event: MouseEvent) => {
      if (!onSelect) return
      const rect = renderer.domElement.getBoundingClientRect()
      const pointer = new THREE.Vector2(
        ((event.clientX - rect.left) / rect.width) * 2 - 1,
        -((event.clientY - rect.top) / rect.height) * 2 + 1,
      )
      raycaster.setFromCamera(pointer, camera)
      const hits = raycaster.intersectObjects(markerGroup.children, true)
      onSelect(hits.length ? ((hits[0].object.userData.key as string) ?? null) : null)
    }

    renderer.domElement.addEventListener('pointerdown', onPointerDown)
    renderer.domElement.addEventListener('pointermove', onPointerMove)
    renderer.domElement.addEventListener('pointerup', onPointerUp)
    renderer.domElement.addEventListener('wheel', onWheel, { passive: false })
    renderer.domElement.addEventListener('click', onClick)

    let frame = 0
    const animate = () => {
      frame = requestAnimationFrame(animate)
      renderer.render(scene, camera)
    }
    animate()

    const onResize = () => {
      if (!mount.clientWidth) return
      camera.aspect = mount.clientWidth / height
      camera.updateProjectionMatrix()
      renderer.setSize(mount.clientWidth, height)
    }
    window.addEventListener('resize', onResize)

    const dispose = () => {
      cancelAnimationFrame(frame)
      window.removeEventListener('resize', onResize)
      renderer.domElement.removeEventListener('pointerdown', onPointerDown)
      renderer.domElement.removeEventListener('pointermove', onPointerMove)
      renderer.domElement.removeEventListener('pointerup', onPointerUp)
      renderer.domElement.removeEventListener('wheel', onWheel)
      renderer.domElement.removeEventListener('click', onClick)
      renderer.dispose()
      mount.removeChild(renderer.domElement)
    }

    stateRef.current = { renderer, scene, camera, content, markerGroup, raycaster, dispose }
    return dispose
    // The scene is created once; content updates happen in the effects below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [height])

  // --- surfaces and toolpaths ----------------------------------------------
  useEffect(() => {
    const state = stateRef.current
    if (!state) return
    clearGroup(state.content)

    if (surface && showSurface) {
      state.content.add(heightFieldMesh(surface.grid, surface.height, '#4b7bb5', 0.95))
      state.content.add(outlineLoop(surface.outline, surface.z_top, '#79c0ff'))
      state.content.add(stockWireframe(surface.stock))
    }
    if (stock && showStock) {
      state.content.add(heightFieldMesh(stock.grid, stock.height, '#8b949e', 0.35, true))
    }
    for (const path of toolpaths) {
      if (!path.visible || !path.moves?.length) continue
      state.content.add(toolpathLines(path.moves, path.color))
    }
  }, [surface, stock, toolpaths, showStock, showSurface])

  // --- feature markers ------------------------------------------------------
  useEffect(() => {
    const state = stateRef.current
    if (!state) return
    clearGroup(state.markerGroup)

    for (const marker of markers) {
      const geometry = new THREE.SphereGeometry(Math.max(2.5, marker.radius), 18, 12)
      const material = new THREE.MeshStandardMaterial({
        color: new THREE.Color(STATUS_COLOR[marker.status] ?? '#8b949e'),
        emissive: new THREE.Color(marker.selected ? '#ffffff' : '#000000'),
        emissiveIntensity: marker.selected ? 0.45 : 0,
        transparent: true,
        opacity: 0.9,
      })
      const mesh = new THREE.Mesh(geometry, material)
      mesh.position.set(marker.position[0], marker.position[1], marker.position[2])
      mesh.userData.key = marker.key
      state.markerGroup.add(mesh)

      if (marker.selected) {
        const ring = new THREE.Mesh(
          new THREE.RingGeometry(marker.radius + 2, marker.radius + 3.5, 32),
          new THREE.MeshBasicMaterial({ color: 0xffffff, side: THREE.DoubleSide, transparent: true, opacity: 0.8 }),
        )
        ring.position.copy(mesh.position)
        ring.userData.key = marker.key
        state.markerGroup.add(ring)
      }
    }
  }, [markers])

  return (
    <div className="viewer" style={{ height }}>
      <div ref={mountRef} className="viewer-canvas" />
      <div className="viewer-legend">
        <span><i style={{ background: '#4b7bb5' }} /> finished surface</span>
        <span><i style={{ background: '#8b949e' }} /> remaining stock</span>
        <span><i style={{ background: '#58a6ff' }} /> feed</span>
        <span><i style={{ background: '#f85149' }} /> rapid</span>
        <span className="viewer-hint">drag to orbit, scroll to zoom, click a feature to select</span>
      </div>
    </div>
  )
}

// ------------------------------------------------------------------- builders
function clearGroup(group: THREE.Group) {
  for (const child of [...group.children]) {
    group.remove(child)
    const mesh = child as THREE.Mesh
    mesh.geometry?.dispose?.()
    const material = mesh.material as THREE.Material | THREE.Material[] | undefined
    if (Array.isArray(material)) material.forEach((m) => m.dispose())
    else material?.dispose?.()
  }
}

/** Triangulate a z(x, y) grid into a surface mesh. */
function heightFieldMesh(
  grid: Surface['grid'],
  height: number[][],
  color: string,
  opacity: number,
  wireframe = false,
): THREE.Mesh {
  const step = grid.pitch * (grid.downsample ?? 1)
  const nx = height.length
  const ny = height[0]?.length ?? 0
  const positions = new Float32Array(nx * ny * 3)

  for (let i = 0; i < nx; i += 1) {
    for (let j = 0; j < ny; j += 1) {
      const index = (i * ny + j) * 3
      positions[index] = grid.x0 + (i + 0.5) * step
      positions[index + 1] = grid.y0 + (j + 0.5) * step
      positions[index + 2] = height[i][j]
    }
  }

  const indices: number[] = []
  for (let i = 0; i < nx - 1; i += 1) {
    for (let j = 0; j < ny - 1; j += 1) {
      const a = i * ny + j
      const b = a + 1
      const c = (i + 1) * ny + j
      const d = c + 1
      indices.push(a, c, b, b, c, d)
    }
  }

  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3))
  geometry.setIndex(indices)
  geometry.computeVertexNormals()

  return new THREE.Mesh(
    geometry,
    new THREE.MeshStandardMaterial({
      color: new THREE.Color(color),
      side: THREE.DoubleSide,
      transparent: opacity < 1,
      opacity,
      wireframe,
      roughness: 0.65,
      metalness: 0.15,
      flatShading: true,
    }),
  )
}

function outlineLoop(points: number[][], z: number, color: string): THREE.Line {
  const vertices = points.map((p) => new THREE.Vector3(p[0], p[1], z))
  if (vertices.length) vertices.push(vertices[0].clone())
  return new THREE.Line(
    new THREE.BufferGeometry().setFromPoints(vertices),
    new THREE.LineBasicMaterial({ color: new THREE.Color(color) }),
  )
}

function stockWireframe(stock: { min: number[]; max: number[] }): THREE.LineSegments {
  const size = [stock.max[0] - stock.min[0], stock.max[1] - stock.min[1], stock.max[2] - stock.min[2]]
  const geometry = new THREE.BoxGeometry(size[0], size[1], size[2])
  geometry.translate(
    stock.min[0] + size[0] / 2,
    stock.min[1] + size[1] / 2,
    stock.min[2] + size[2] / 2,
  )
  return new THREE.LineSegments(
    new THREE.EdgesGeometry(geometry),
    new THREE.LineBasicMaterial({ color: 0x30363d, transparent: true, opacity: 0.8 }),
  )
}

/** Toolpath as coloured line segments: rapid, feed and plunge read differently. */
function toolpathLines(moves: Move[], override?: string): THREE.LineSegments {
  const positions: number[] = []
  const colors: number[] = []
  let previous: [number, number, number] | null = null
  const fixed = override ? new THREE.Color(override) : null

  for (const move of moves) {
    if (move.t === 'dwell') continue
    const point: [number, number, number] =
      move.t === 'drill'
        ? [move.x ?? 0, move.y ?? 0, Number(move.cycle?.z_depth ?? 0)]
        : [move.x ?? previous?.[0] ?? 0, move.y ?? previous?.[1] ?? 0, move.z ?? previous?.[2] ?? 0]

    if (previous) {
      const color = fixed ?? (move.t === 'rapid' ? RAPID_COLOR : move.t === 'plunge' ? PLUNGE_COLOR : FEED_COLOR)
      positions.push(previous[0], previous[1], previous[2], point[0], point[1], point[2])
      colors.push(color.r, color.g, color.b, color.r, color.g, color.b)
    }
    previous = point
  }

  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3))
  geometry.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3))
  return new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.95 }))
}
