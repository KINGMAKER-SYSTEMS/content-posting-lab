import { BrowserRouter, Routes, Route, Link, useLocation } from 'react-router-dom'

function GeneratePage() {
  return (
    <div className="p-8">
      <h1 className="text-3xl font-bold mb-4">Video Generation</h1>
      <p className="text-slate-400">Generate AI videos from text prompts</p>
    </div>
  )
}

function CaptionsPage() {
  return (
    <div className="p-8">
      <h1 className="text-3xl font-bold mb-4">Caption Scraping</h1>
      <p className="text-slate-400">Scrape captions from TikTok profiles</p>
    </div>
  )
}

function BurnPage() {
  return (
    <div className="p-8">
      <h1 className="text-3xl font-bold mb-4">Caption Burning</h1>
      <p className="text-slate-400">Burn captions onto videos</p>
    </div>
  )
}

function HomePage() {
  return (
    <div className="p-8">
      <h1 className="text-3xl font-bold mb-4">Content Posting Lab</h1>
      <p className="text-slate-400">Unified video generation and captioning pipeline</p>
    </div>
  )
}

function TabNav() {
  const location = useLocation()
  
  const tabs = [
    { path: '/', label: 'Home' },
    { path: '/generate', label: 'Generate' },
    { path: '/captions', label: 'Captions' },
    { path: '/burn', label: 'Burn' },
  ]

  return (
    <nav className="border-b border-slate-700 bg-slate-900">
      <div className="flex gap-1 px-4">
        {tabs.map((tab) => (
          <Link
            key={tab.path}
            to={tab.path}
            className={`px-4 py-3 font-medium transition-colors ${
              location.pathname === tab.path
                ? 'border-b-2 border-purple-500 text-purple-400'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            {tab.label}
          </Link>
        ))}
      </div>
    </nav>
  )
}

function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen bg-slate-950 text-slate-100">
        <TabNav />
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/generate" element={<GeneratePage />} />
          <Route path="/captions" element={<CaptionsPage />} />
          <Route path="/burn" element={<BurnPage />} />
        </Routes>
      </div>
    </BrowserRouter>
  )
}

export default App
