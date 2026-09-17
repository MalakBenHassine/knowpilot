import { MessageSquare } from 'lucide-react'
import { useNavigate } from 'react-router'
import { MessageList } from '../components/chat/MessageList'
import { QuestionInput } from '../components/chat/QuestionInput'
import { Button } from '../components/ui/Button'
import { EmptyState } from '../components/ui/EmptyState'
import { useChat } from '../hooks/useChat'
import { useDocumentsContext } from '../hooks/documents-context'

const SUGGESTIONS = [
  'What are the key obligations in this contract?',
  'Summarise the main points of my notes.',
  'What does the document say about deadlines?',
]

export function ChatPage() {
  const { turns, isBusy, ask, retry } = useChat()
  const { readyCount, isLoading } = useDocumentsContext()
  const navigate = useNavigate()

  const hasKnowledge = readyCount > 0
  const disabledReason =
    !isLoading && !hasKnowledge ? 'Upload a document before asking a question' : undefined

  return (
    <div className="flex h-[calc(100dvh-3.5rem)] flex-col">
      <header className="border-b border-line px-4 py-4 sm:px-6">
        <div className="mx-auto max-w-3xl">
          <h1 className="text-heading font-semibold text-ink">Ask your documents</h1>
          <p className="mt-0.5 text-caption text-ink-muted">
            Answers are grounded in your uploaded sources.
          </p>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {turns.length === 0 ? (
          <div className="mx-auto max-w-3xl px-4 py-10">
            {hasKnowledge ? (
              <div className="animate-fade-in">
                <EmptyState
                  icon={<MessageSquare size={20} />}
                  title="Ask your first question"
                  description={`${readyCount} document(s) are indexed and ready to be searched.`}
                />
                <div className="mt-6 flex flex-wrap justify-center gap-2">
                  {SUGGESTIONS.map((suggestion) => (
                    <button
                      key={suggestion}
                      type="button"
                      onClick={() => void ask(suggestion)}
                      className="rounded-md border border-line bg-surface px-3 py-1.5 text-caption text-ink-muted transition-colors duration-150 hover:border-line-strong hover:text-ink"
                    >
                      {suggestion}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              <EmptyState
                icon={<MessageSquare size={20} />}
                title="No knowledge to search yet"
                description="KnowPilot only answers from your own documents. Upload one to get started."
                action={<Button onClick={() => void navigate('/documents')}>Upload a document</Button>}
              />
            )}
          </div>
        ) : (
          <MessageList turns={turns} onRetry={(id) => void retry(id)} />
        )}
      </div>

      <QuestionInput
        onSubmit={(question) => void ask(question)}
        isBusy={isBusy}
        disabledReason={disabledReason}
      />
    </div>
  )
}
