import { useEffect, useRef } from 'react'
import type { ChatTurn } from '../../types/chat'
import type { Source } from '../../types/source'
import { AssistantMessage } from './AssistantMessage'
import { UserMessage } from './UserMessage'

export function MessageList({
  turns,
  onRetry,
  onOpenSource,
}: {
  turns: ChatTurn[]
  onRetry: (id: string) => void
  onOpenSource?: (source: Source) => void
}) {
  const bottomRef = useRef<HTMLDivElement>(null)

  // Keep the newest turn in view as the conversation grows.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' })
  }, [turns])

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-6">
      {turns.map((turn) =>
        turn.role === 'user' ? (
          <UserMessage key={turn.id} turn={turn} />
        ) : (
          <AssistantMessage key={turn.id} turn={turn} onRetry={onRetry} onOpenSource={onOpenSource} />
        ),
      )}
      <div ref={bottomRef} />
    </div>
  )
}
