import { UploadCloud } from 'lucide-react'
import { useRef, useState, type DragEvent } from 'react'
import { cn } from '../../lib/cn'
import { formatBytes } from '../../lib/format'
import { ACCEPTED_MIME_TYPES, MAX_UPLOAD_BYTES } from '../../types/document'
import { Button } from '../ui/Button'

/** Client-side validation is for feedback only — the backend validates again. */
function rejectionReason(file: File): string | null {
  const isAccepted =
    (ACCEPTED_MIME_TYPES as readonly string[]).includes(file.type) ||
    file.name.toLowerCase().endsWith('.txt') ||
    file.name.toLowerCase().endsWith('.pdf')
  if (!isAccepted) return `${file.name} is not a PDF or TXT file.`
  if (file.size === 0) return `${file.name} is empty.`
  if (file.size > MAX_UPLOAD_BYTES) {
    return `${file.name} is ${formatBytes(file.size)}, over the ${formatBytes(MAX_UPLOAD_BYTES)} limit.`
  }
  return null
}

export function UploadZone({ onFiles }: { onFiles: (files: File[]) => void }) {
  const [isDragging, setIsDragging] = useState(false)
  const [rejected, setRejected] = useState<string[]>([])
  const inputRef = useRef<HTMLInputElement>(null)

  function handleFiles(fileList: FileList | null) {
    if (!fileList || fileList.length === 0) return
    const files = Array.from(fileList)
    const problems = files.map(rejectionReason)
    const accepted = files.filter((_, index) => problems[index] === null)
    setRejected(problems.filter((problem): problem is string => problem !== null))
    if (accepted.length > 0) onFiles(accepted)
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setIsDragging(false)
    handleFiles(event.dataTransfer.files)
  }

  return (
    <div>
      <div
        onDragOver={(event) => {
          event.preventDefault()
          setIsDragging(true)
        }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={handleDrop}
        className={cn(
          'flex flex-col items-center justify-center rounded-lg border border-dashed px-6 py-10 text-center',
          'transition-[background-color,border-color,transform] duration-150',
          isDragging
            ? 'scale-[1.005] border-accent bg-accent-soft'
            : 'border-line bg-surface hover:border-line-strong',
        )}
      >
        <UploadCloud
          size={22}
          className={cn('mb-3 transition-colors', isDragging ? 'text-accent' : 'text-ink-subtle')}
        />
        <p className="text-body font-medium text-ink">
          {isDragging ? 'Drop your files here' : 'Drag and drop your documents'}
        </p>
        <p className="mt-1 text-caption text-ink-muted">
          PDF or TXT · up to {formatBytes(MAX_UPLOAD_BYTES)} per file
        </p>
        <Button
          variant="secondary"
          size="sm"
          className="mt-4"
          onClick={() => inputRef.current?.click()}
        >
          Choose files
        </Button>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".pdf,.txt,application/pdf,text/plain"
          className="sr-only"
          aria-label="Upload documents"
          onChange={(event) => {
            handleFiles(event.target.files)
            event.target.value = ''
          }}
        />
      </div>

      {rejected.length > 0 ? (
        <ul className="mt-3 space-y-1" role="alert">
          {rejected.map((message) => (
            <li key={message} className="text-caption text-danger">
              {message}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}
