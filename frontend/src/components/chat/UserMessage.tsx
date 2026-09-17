import type { UserTurn } from '../../types/chat'

export function UserMessage({ turn }: { turn: UserTurn }) {
  return (
    <div className="flex justify-end animate-rise">
      <p className="max-w-[85%] rounded-lg rounded-br-sm border border-line bg-surface-raised px-4 py-2.5 text-body whitespace-pre-wrap text-ink shadow-xs sm:max-w-[70%]">
        {turn.question}
      </p>
    </div>
  )
}
