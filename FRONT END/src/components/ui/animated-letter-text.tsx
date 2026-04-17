"use client"

import React from "react"
import { cn } from "../../lib/utils"

interface AnimatedLetterTextProps {
  text: string
  letterToReplace?: string
  className?: string
}

export function AnimatedLetterText({ text = "VOICE GUARD", letterToReplace = "O", className }: AnimatedLetterTextProps) {
  let keyIndex = 0

  const lowerText = text.toLowerCase()
  const lowerLetter = letterToReplace.toLowerCase()
  const replaceIndex = lowerText.indexOf(lowerLetter)

  const textClass = "text-[#2D2D2D]"

  if (replaceIndex === -1) {
    return <span className={cn("font-extrabold tracking-wide", textClass, className)}>{text}</span>
  }

  const before = text.slice(0, replaceIndex)
  const after = text.slice(replaceIndex + 1)

  return (
    <span className={cn("inline-flex items-center font-extrabold tracking-wide", className)}>
      {before && <span key={keyIndex++} className={textClass}>{before}</span>}

      <span className="relative inline-flex items-center justify-center mx-[0.05em] w-[0.8em] h-[0.8em]">
        {/* Clean, solid 'O' shape without blur or shadow */}
        <svg viewBox="0 0 100 100" className="absolute inset-0 w-full h-full">
          <circle
            cx="50"
            cy="50"
            r="38"
            fill="none"
            stroke="#2D2D2D"
            strokeWidth="20"
          />
        </svg>
      </span>

      {after && <span key={keyIndex++} className={textClass}>{after}</span>}
    </span>
  )
}
