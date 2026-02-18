import React, { useState, useEffect, useRef } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';
import type { Provider, Job, VideoEntry } from '../types/api';
import { StatusChip, EmptyState } from '../components';

export function GeneratePage() {
  const { activeProject } = useWorkflowStore();
  const [providers, setProviders] = useState<Provider[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  
  // Form State
  const [prompt, setPrompt] = useState('');
  const [selectedProvider, setSelectedProvider] = useState('');
  const [count, setCount] = useState(1);
  const [duration, setDuration] = useState(10);
  const [aspectRatio, setAspectRatio] = useState('9:16');
  const [resolution, setResolution] = useState('720p');
  const [mediaFile, setMediaFile] = useState<File | null>(null);
  
  const fileInputRef = useRef<HTMLInputElement>(null);
  const activePolls = useRef<Set<string>>(new Set());

  // Load providers on mount
  useEffect(() => {
    fetch('/api/video/providers')
      .then(res => res.json())
      .then(data => {
        setProviders(data);
        if (data.length > 0) setSelectedProvider(data[0].id);
      })
      .catch(err => console.error('Failed to load providers', err));
  }, []);

  // Poll active jobs
  useEffect(() => {
    const pollJob = async (jobId: string) => {
      if (activePolls.current.has(jobId)) return;
      activePolls.current.add(jobId);

      const checkStatus = async () => {
        try {
          const res = await fetch(`/api/video/jobs/${jobId}`);
          if (!res.ok) throw new Error('Failed to fetch job');
          const job: Job = await res.json();
          
          setJobs(prev => {
            const exists = prev.find(j => j.id === job.id);
            if (exists) {
              return prev.map(j => j.id === job.id ? job : j);
            }
            return [job, ...prev];
          });

          const allDone = job.videos.every(v => v.status === 'done' || v.status === 'failed');
          if (!allDone) {
            setTimeout(checkStatus, 2000);
          } else {
            activePolls.current.delete(jobId);
          }
        } catch (err) {
          console.error('Polling error', err);
          setTimeout(checkStatus, 3000);
        }
      };
      
      checkStatus();
    };

    // Resume polling for any incomplete jobs in state
    jobs.forEach(job => {
      const allDone = job.videos.every(v => v.status === 'done' || v.status === 'failed');
      if (!allDone) {
        pollJob(job.id);
      }
    });
  }, [jobs]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!activeProject) return;
    
    setLoading(true);
    setError(null);

    const formData = new FormData();
    formData.append('prompt', prompt);
    formData.append('provider', selectedProvider);
    formData.append('count', count.toString());
    formData.append('duration', duration.toString());
    formData.append('aspect_ratio', aspectRatio);
    formData.append('resolution', resolution);
    if (mediaFile) {
      formData.append('media', mediaFile);
    }

    try {
      const res = await fetch('/api/video/generate', {
        method: 'POST',
        body: formData,
      });
      
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text);
      }

      const data = await res.json();
      // Add placeholder job immediately
      const newJob: Job = {
        id: data.job_id,
        prompt,
        provider: selectedProvider,
        count,
        videos: Array(count).fill({ index: 0, status: 'queued' } as VideoEntry)
      };
      setJobs(prev => [newJob, ...prev]);
      
      // Reset form
      setPrompt('');
      setMediaFile(null);
      if (fileInputRef.current) fileInputRef.current.value = '';
      
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      setMediaFile(e.target.files[0]);
    }
  };

  if (!activeProject) {
    return (
      <div className="h-full flex items-center justify-center">
        <EmptyState
          icon="📁"
          title="No Project Selected"
          description="Please select or create a project to start generating videos."
        />
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col lg:flex-row overflow-hidden">
      {/* Sidebar - Controls */}
      <div className="w-full lg:w-[400px] bg-slate-900 border-r border-slate-800 p-6 overflow-y-auto flex-shrink-0">
        <h2 className="text-xl font-bold mb-6 text-white">Generate Video</h2>
        
        <form onSubmit={handleSubmit} className="space-y-6">
          <div>
            <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
              Provider
            </label>
            <select
              value={selectedProvider}
              onChange={(e) => setSelectedProvider(e.target.value)}
              className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-slate-200 focus:outline-none focus:border-blue-500"
            >
              {providers.map(p => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.pricing})
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
              Prompt
            </label>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Describe the video you want to generate..."
              className="w-full h-32 bg-slate-800 border border-slate-700 rounded-lg p-3 text-slate-200 focus:outline-none focus:border-blue-500 resize-none"
              required
            />
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
              Reference Media (Optional)
            </label>
            <div 
              className={`border-2 border-dashed rounded-lg p-4 text-center cursor-pointer transition-colors ${
                mediaFile ? 'border-blue-500 bg-blue-500/10' : 'border-slate-700 hover:border-slate-600'
              }`}
              onClick={() => fileInputRef.current?.click()}
            >
              <input
                type="file"
                ref={fileInputRef}
                onChange={handleFileChange}
                className="hidden"
                accept="image/*,video/*"
              />
              {mediaFile ? (
                <div className="text-sm text-blue-400 font-medium truncate">
                  {mediaFile.name}
                </div>
              ) : (
                <div className="text-slate-500 text-sm">
                  Click to upload image or video
                </div>
              )}
            </div>
            {mediaFile && (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  setMediaFile(null);
                  if (fileInputRef.current) fileInputRef.current.value = '';
                }}
                className="text-xs text-red-400 mt-1 hover:text-red-300"
              >
                Remove file
              </button>
            )}
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                Count
              </label>
              <input
                type="number"
                min="1"
                max="10"
                value={count}
                onChange={(e) => setCount(parseInt(e.target.value))}
                className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-slate-200 focus:outline-none focus:border-blue-500"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                Duration (s)
              </label>
              <input
                type="number"
                min="1"
                max="15"
                value={duration}
                onChange={(e) => setDuration(parseInt(e.target.value))}
                className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-slate-200 focus:outline-none focus:border-blue-500"
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                Aspect Ratio
              </label>
              <select
                value={aspectRatio}
                onChange={(e) => setAspectRatio(e.target.value)}
                className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-slate-200 focus:outline-none focus:border-blue-500"
              >
                <option value="9:16">9:16 TikTok</option>
                <option value="16:9">16:9 Landscape</option>
                <option value="1:1">1:1 Square</option>
                <option value="4:3">4:3</option>
                <option value="3:4">3:4</option>
              </select>
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                Resolution
              </label>
              <select
                value={resolution}
                onChange={(e) => setResolution(e.target.value)}
                className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-slate-200 focus:outline-none focus:border-blue-500"
              >
                <option value="720p">720p HD</option>
                <option value="480p">480p SD</option>
              </select>
            </div>
          </div>

          {error && (
            <div className="p-3 bg-red-500/10 border border-red-500/20 rounded-lg text-red-400 text-sm">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading || !selectedProvider}
            className={`w-full py-3 px-4 rounded-lg font-semibold text-white transition-all ${
              loading || !selectedProvider
                ? 'bg-slate-700 cursor-not-allowed opacity-50'
                : 'bg-blue-600 hover:bg-blue-500 active:scale-[0.98]'
            }`}
          >
            {loading ? 'Submitting...' : 'Generate Videos'}
          </button>
        </form>
      </div>

      {/* Main Panel - Results */}
      <div className="flex-1 bg-slate-950 p-8 overflow-y-auto">
        <div className="max-w-6xl mx-auto">
          <h2 className="text-2xl font-bold mb-8 text-white">Generated Videos</h2>

          {jobs.length === 0 ? (
            <EmptyState
              icon="🎬"
              title="No Videos Yet"
              description="Enter a prompt and click Generate to start creating videos."
            />
          ) : (
            <div className="space-y-8">
              {jobs.map(job => (
                <div key={job.id} className="bg-slate-900/50 border border-slate-800 rounded-xl p-6">
                  <div className="flex items-start justify-between mb-6">
                    <div>
                      <div className="flex items-center gap-3 mb-2">
                        <span className="text-xs font-mono bg-slate-800 text-slate-400 px-2 py-1 rounded">
                          {job.provider}
                        </span>
                        <span className="text-xs text-slate-500">
                          ID: {job.id}
                        </span>
                      </div>
                      <p className="text-slate-200 font-medium italic">"{job.prompt}"</p>
                    </div>
                    
                    {job.videos.some(v => v.status === 'done') && (
                      <a
                        href={`/api/video/jobs/${job.id}/download-all`}
                        className="text-sm bg-green-600/10 text-green-400 hover:bg-green-600/20 px-3 py-1.5 rounded-lg transition-colors font-medium"
                        download
                      >
                        Download All
                      </a>
                    )}
                  </div>

                  <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
                    {job.videos.map((video, idx) => (
                      <div key={`${job.id}-${idx}`} className="group relative bg-black rounded-lg overflow-hidden aspect-[9/16] border border-slate-800 hover:border-slate-600 transition-colors">
                        {video.status === 'done' && video.file ? (
                          <>
                            <video
                              src={video.file}
                              className="w-full h-full object-cover"
                              controls
                              playsInline
                              muted
                            />
                            <div className="absolute bottom-0 left-0 right-0 p-3 bg-gradient-to-t from-black/80 to-transparent opacity-0 group-hover:opacity-100 transition-opacity flex justify-end">
                              <a
                                href={video.file}
                                download
                                className="text-xs bg-white/10 hover:bg-white/20 text-white px-2 py-1 rounded backdrop-blur-sm transition-colors"
                              >
                                Download
                              </a>
                            </div>
                          </>
                        ) : (
                          <div className="absolute inset-0 flex flex-col items-center justify-center p-4 text-center">
                            {video.status === 'failed' ? (
                              <>
                                <span className="text-2xl mb-2">❌</span>
                                <span className="text-xs text-red-400 font-medium">Failed</span>
                                {video.error && (
                                  <span className="text-[10px] text-red-500/70 mt-1 line-clamp-2">
                                    {video.error}
                                  </span>
                                )}
                              </>
                            ) : (
                              <>
                                <div className="w-8 h-8 border-2 border-blue-500/30 border-t-blue-500 rounded-full animate-spin mb-3" />
                                <StatusChip status={video.status === 'queued' ? 'pending' : 'processing'} />
                              </>
                            )}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
