import React, { useState, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { useWorkflowStore } from '../stores/workflowStore';
import type { Provider, Job } from '../types/api';
import { StatusChip, EmptyState } from '../components';

export function GeneratePage() {
  const navigate = useNavigate();
  const {
    activeProject,
    addGeneratedVideo,
    addNotification,
    setVideoRunningCount,
    incrementBurnReadyCount,
    primeBurnSelection,
  } = useWorkflowStore();
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

  useEffect(() => {
    if (!activeProject) {
      setProviders([]);
      setSelectedProvider('');
      return;
    }

    let isCancelled = false;

    fetch('/api/video/providers')
      .then(async (res) => {
        if (!res.ok) {
          const text = await res.text();
          throw new Error(text || `Failed to load providers (${res.status})`);
        }
        return res.json();
      })
      .then((data: Provider[]) => {
        if (isCancelled) {
          return;
        }
        setProviders(data);
        setSelectedProvider(data.length > 0 ? data[0].id : '');
      })
      .catch((err: unknown) => {
        if (isCancelled) {
          return;
        }
        const message = err instanceof Error ? err.message : 'Failed to load providers';
        setError(message);
        addNotification('error', message);
      });

    return () => {
      isCancelled = true;
    };
  }, [activeProject, addNotification]);

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

          const allDone = job.videos.every(v => v.status === 'done' || v.status === 'failed' || v.status === 'error');
          if (!allDone) {
            setTimeout(checkStatus, 2000);
          } else {
            activePolls.current.delete(jobId);
            const successCount = job.videos.filter(v => v.status === 'done').length;
            if (successCount > 0) {
              addGeneratedVideo(jobId);
              incrementBurnReadyCount(successCount);
              addNotification('success', `Generated ${successCount} videos for "${job.prompt.substring(0, 20)}..."`);
              window.dispatchEvent(new Event('burn:refresh-request'));
            } else {
              addNotification('error', `Failed to generate videos for "${job.prompt.substring(0, 20)}..."`);
            }
          }
        } catch (err) {
          const message = err instanceof Error ? err.message : 'Polling error';
          setError(message);
          setTimeout(checkStatus, 3000);
        }
      };
      
      checkStatus();
    };

    jobs.forEach(job => {
      const allDone = job.videos.every(v => v.status === 'done' || v.status === 'failed' || v.status === 'error');
      if (!allDone) {
        pollJob(job.id);
      }
    });

    const runningCount = jobs.filter((job) =>
      job.videos.some((video) => video.status !== 'done' && video.status !== 'failed' && video.status !== 'error'),
    ).length;
    setVideoRunningCount(runningCount);

  }, [addNotification, addGeneratedVideo, incrementBurnReadyCount, jobs, setVideoRunningCount]);

  useEffect(() => {
    return () => {
      setVideoRunningCount(0);
    };
  }, [setVideoRunningCount]);

  const handleSubmit: React.FormEventHandler<HTMLFormElement> = async (e) => {
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
    formData.append('project', activeProject.name);
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
      const newJob: Job = {
        id: data.job_id,
        prompt,
        provider: selectedProvider,
        count,
        project: activeProject.name,
        videos: Array.from({ length: count }, (_, index) => ({ index, status: 'queued' as const }))
      };
      setJobs(prev => [newJob, ...prev]);

      setPrompt('');
      setMediaFile(null);
      if (fileInputRef.current) fileInputRef.current.value = '';
      
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to submit job';
      setError(message);
    } finally {
      setLoading(false);
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      setMediaFile(e.target.files[0]);
    }
  };

  const sendSelectionToBurn = (job: Job) => {
    const videoPaths = job.videos.reduce<string[]>((paths, video) => {
      if (video.status === 'done' && typeof video.file === 'string') {
        paths.push(video.file);
      }
      return paths;
    }, []);

    if (videoPaths.length === 0) {
      addNotification('info', 'No completed videos available to preselect in Burn yet.');
      return;
    }

    primeBurnSelection({ videoPaths });
    navigate('/burn');
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
      <div className="w-full lg:w-[400px] bg-black/20 border-r border-white/10 p-6 overflow-y-auto flex-shrink-0 backdrop-blur-sm">
        <h2 className="text-xl font-bold mb-6 text-white">Generate Video</h2>
        
        <form onSubmit={handleSubmit} className="space-y-6">
          <div>
            <label className="label">
              Provider
            </label>
            <select
              value={selectedProvider}
              onChange={(e) => setSelectedProvider(e.target.value)}
              className="input"
            >
              {providers.map(p => (
                <option key={p.id} value={p.id} className="bg-charcoal">
                  {p.name} ({p.pricing})
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="label">
              Prompt
            </label>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Describe the video you want to generate..."
              className="input h-32 resize-none"
              required
            />
          </div>

          <div>
            <label className="label">
              Reference Media (Optional)
            </label>
            <div 
              className={`border-2 border-dashed rounded-lg p-4 text-center cursor-pointer transition-colors ${
                mediaFile ? 'border-purple-500 bg-purple-500/10' : 'border-white/10 hover:border-white/20 hover:bg-white/5'
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
                <div className="text-sm text-purple-400 font-medium truncate">
                  {mediaFile.name}
                </div>
              ) : (
                <div className="text-gray-500 text-sm">
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
              <label className="label">
                Count
              </label>
              <input
                type="number"
                min="1"
                max="10"
                value={count}
                onChange={(e) => setCount(parseInt(e.target.value))}
                className="input"
              />
            </div>
            <div>
              <label className="label">
                Duration (s)
              </label>
              <input
                type="number"
                min="1"
                max="15"
                value={duration}
                onChange={(e) => setDuration(parseInt(e.target.value))}
                className="input"
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="label">
                Aspect Ratio
              </label>
              <select
                value={aspectRatio}
                onChange={(e) => setAspectRatio(e.target.value)}
                className="input"
              >
                <option value="9:16" className="bg-charcoal">9:16 TikTok</option>
                <option value="16:9" className="bg-charcoal">16:9 Landscape</option>
                <option value="1:1" className="bg-charcoal">1:1 Square</option>
                <option value="4:3" className="bg-charcoal">4:3</option>
                <option value="3:4" className="bg-charcoal">3:4</option>
              </select>
            </div>
            <div>
              <label className="label">
                Resolution
              </label>
              <select
                value={resolution}
                onChange={(e) => setResolution(e.target.value)}
                className="input"
              >
                <option value="720p" className="bg-charcoal">720p HD</option>
                <option value="480p" className="bg-charcoal">480p SD</option>
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
            className={`w-full btn btn-primary ${
              loading || !selectedProvider ? 'opacity-50 cursor-not-allowed' : ''
            }`}
          >
            {loading ? 'Submitting...' : 'Generate Videos'}
          </button>
        </form>
      </div>

      {/* Main Panel - Results */}
      <div className="flex-1 p-8 overflow-y-auto">
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
                <div key={job.id} className="card">
                  <div className="flex items-start justify-between mb-6">
                    <div>
                      <div className="flex items-center gap-3 mb-2">
                        <span className="text-xs font-mono bg-white/10 text-gray-300 px-2 py-1 rounded">
                          {job.provider}
                        </span>
                        <span className="text-xs text-gray-500">
                          ID: {job.id}
                        </span>
                      </div>
                      <p className="text-gray-200 font-medium italic">"{job.prompt}"</p>
                    </div>
                    
                    {job.videos.some(v => v.status === 'done') && (
                      <div className="flex gap-2">
                        <button
                          onClick={() => sendSelectionToBurn(job)}
                          className="btn btn-secondary text-sm py-1.5 px-3"
                        >
                          <span>Use in Burn</span>
                          <span className="ml-1 text-lg">→</span>
                        </button>
                        <a
                          href={`/api/video/jobs/${job.id}/download-all`}
                          className="btn btn-secondary text-sm py-1.5 px-3 text-green-400 hover:text-green-300 border-green-500/20 hover:bg-green-500/10"
                          download
                        >
                          Download All
                        </a>
                      </div>
                    )}
                  </div>

                  <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
                    {job.videos.map((video, idx) => (
                      <div key={`${job.id}-${idx}`} className="group relative bg-black rounded-lg overflow-hidden aspect-[9/16] border border-white/10 hover:border-white/30 transition-colors">
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
                                <div className="w-8 h-8 border-2 border-purple-500/30 border-t-purple-500 rounded-full animate-spin mb-3" />
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
