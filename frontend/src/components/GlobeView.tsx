import { useEffect, useRef, useMemo, useCallback, useState } from 'react'
import Globe from 'react-globe.gl'
import type { WeatherForecast, WeatherSignal } from '../types'

interface Props {
  forecasts: WeatherForecast[]
  signals: WeatherSignal[]
}

interface CityMarker {
  lat: number
  lng: number
  name: string
  key: string
  forecast: WeatherForecast | null
  bestSignal: WeatherSignal | null
  hasActionable: boolean
}

const CITIES: Record<string, { lat: number; lng: number; name: string }> = {
  nyc: { lat: 40.7128, lng: -74.006, name: 'NYC' },
  chicago: { lat: 41.8781, lng: -87.6298, name: 'CHI' },
  miami: { lat: 25.7617, lng: -80.1918, name: 'MIA' },
  los_angeles: { lat: 34.0522, lng: -118.2437, name: 'LA' },
  denver: { lat: 39.7392, lng: -104.9903, name: 'DEN' },
  boston: { lat: 42.3656, lng: -71.0096, name: 'BOS' },
}

export function GlobeView({ forecasts, signals }: Props) {
  const globeRef = useRef<any>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  // react-globe.gl's own auto-sizing (width/height left undefined) doesn't
  // reliably pick up a container sized by percentage-height flex/grid layout
  // — it can fall back to the full window size instead, rendering a canvas
  // far larger than its box and cropping the visible globe. Measuring the
  // container directly and passing explicit pixel dimensions sidesteps that.
  const [size, setSize] = useState({ width: 0, height: 0 })

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const update = () => setSize({ width: el.clientWidth, height: el.clientHeight })
    update()
    const observer = new ResizeObserver(update)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  const markers: CityMarker[] = useMemo(() => {
    return Object.entries(CITIES).map(([key, city]) => {
      const cityForecasts = forecasts.filter(f => f.city_key === key)
      const citySignals = signals.filter(s => s.city_key === key)
      const actionableSignals = citySignals.filter(s => s.actionable)
      const bestSignal = actionableSignals.length > 0
        ? actionableSignals.reduce((a, b) => Math.abs(a.edge) > Math.abs(b.edge) ? a : b)
        : citySignals.length > 0
          ? citySignals.reduce((a, b) => Math.abs(a.edge) > Math.abs(b.edge) ? a : b)
          : null

      // A city can have more than one forecast row (today's + tomorrow's
      // markets both live at once) — prefer the one matching the best
      // signal's date so the globe doesn't pair a signal with the wrong
      // day's forecast; fall back to the earliest date otherwise.
      const forecast = (
        (bestSignal && cityForecasts.find(f => f.target_date === bestSignal.target_date)) ||
        [...cityForecasts].sort((a, b) => a.target_date.localeCompare(b.target_date))[0] ||
        null
      )

      return {
        lat: city.lat,
        lng: city.lng,
        name: city.name,
        key,
        forecast,
        bestSignal,
        hasActionable: actionableSignals.length > 0,
      }
    })
  }, [forecasts, signals])

  useEffect(() => {
    if (globeRef.current) {
      globeRef.current.pointOfView({ lat: 39.5, lng: -98.35, altitude: 2.2 }, 1000)
      globeRef.current.controls().autoRotate = true
      globeRef.current.controls().autoRotateSpeed = 0.3
      globeRef.current.controls().enableZoom = false
    }
  }, [])

  const handleInteraction = useCallback(() => {
    if (globeRef.current) {
      globeRef.current.controls().autoRotate = false
      setTimeout(() => {
        if (globeRef.current) {
          globeRef.current.controls().autoRotate = true
        }
      }, 5000)
    }
  }, [])

  const markerElement = useCallback((d: object) => {
    const marker = d as CityMarker
    const el = document.createElement('div')
    el.className = 'city-marker'

    const dotColor = marker.hasActionable ? '#22c55e' : marker.bestSignal ? '#d97706' : '#525252'

    const dot = document.createElement('div')
    dot.className = 'marker-dot'
    dot.style.backgroundColor = dotColor
    dot.style.color = dotColor
    el.appendChild(dot)

    const label = document.createElement('div')
    label.className = 'marker-label'

    const nameSpan = document.createElement('div')
    nameSpan.className = 'marker-name'
    nameSpan.textContent = marker.name
    label.appendChild(nameSpan)

    if (marker.forecast) {
      const tempSpan = document.createElement('div')
      tempSpan.className = 'marker-temp'
      tempSpan.style.color = '#e5e5e5'
      tempSpan.textContent = `${marker.forecast.effective_mean_high.toFixed(0)}F`
      label.appendChild(tempSpan)
    }

    if (marker.bestSignal) {
      const edgeSpan = document.createElement('div')
      edgeSpan.className = 'marker-edge'
      const edgeVal = (marker.bestSignal.edge * 100).toFixed(1)
      edgeSpan.style.color = marker.bestSignal.edge > 0 ? '#22c55e' : '#dc2626'
      edgeSpan.textContent = `${marker.bestSignal.edge > 0 ? '+' : ''}${edgeVal}%`
      label.appendChild(edgeSpan)
    }

    el.appendChild(label)
    return el
  }, [])

  return (
    <div ref={containerRef} className="globe-container w-full h-full">
      {size.width > 0 && size.height > 0 && (
        <Globe
          ref={globeRef}
          globeImageUrl="//unpkg.com/three-globe/example/img/earth-night.jpg"
          backgroundColor="rgba(0,0,0,0)"
          atmosphereColor="#1a1a2e"
          atmosphereAltitude={0.15}
          htmlElementsData={markers}
          htmlElement={markerElement}
          htmlAltitude={0.01}
          onGlobeClick={handleInteraction}
          width={size.width}
          height={size.height}
        />
      )}
    </div>
  )
}
