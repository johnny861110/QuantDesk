import { useCallback, useState } from 'react'
import type { Signal } from '../types'

const STORAGE_KEY = 'quantdesk_history'
const MAX_ENTRIES = 20

export interface HistoryEntry {
  query: string
  timestamp: number
  signal?: Signal
}

function load(): HistoryEntry[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? (JSON.parse(raw) as HistoryEntry[]) : []
  } catch {
    return []
  }
}

function save(entries: HistoryEntry[]) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(entries.slice(0, MAX_ENTRIES)))
}

export function useQueryHistory() {
  const [history, setHistory] = useState<HistoryEntry[]>(load)

  const addQuery = useCallback((query: string, signal?: Signal) => {
    setHistory(prev => {
      const next = [{ query, timestamp: Date.now(), signal }, ...prev.filter(e => e.query !== query)]
      save(next)
      return next.slice(0, MAX_ENTRIES)
    })
  }, [])

  const clearHistory = useCallback(() => {
    localStorage.removeItem(STORAGE_KEY)
    setHistory([])
  }, [])

  return { history, addQuery, clearHistory }
}
