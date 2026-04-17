import { cn } from "../../lib/utils";
import React from "react";

function hexToRgba(hex: string, alpha: number = 1): string {
  let hexValue = hex.replace("#", "");
 
  if (hexValue.length === 3) {
    hexValue = hexValue
      .split("")
      .map((char) => char + char)
      .join("");
  }
 
  const r = parseInt(hexValue.substring(0, 2), 16);
  const g = parseInt(hexValue.substring(2, 4), 16);
  const b = parseInt(hexValue.substring(4, 6), 16);
 
  if (isNaN(r) || isNaN(g) || isNaN(b)) {
    console.error("Invalid hex color:", hex);
    return "rgba(0, 0, 0, 1)";
  }
 
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
 
export function GlowingButton({
  children,
  className,
  glowColor = "#a3e635",
  onClick,
  disabled
}: {
  children: React.ReactNode;
  className?: string;
  glowColor?: string;
  onClick?: () => void;
  disabled?: boolean;
}) {
  const glowColorRgba = hexToRgba(glowColor);
  const glowColorVia = hexToRgba(glowColor, 0.075);
  const glowColorTo = hexToRgba(glowColor, 0.2);
 
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={
        {
          "--glow-color": glowColorRgba,
          "--glow-color-via": glowColorVia,
          "--glow-color-to": glowColorTo,
        } as React.CSSProperties
      }
      className={cn(
        "w-full h-full text-2xl font-black rounded-[32px] border-2 flex items-center justify-center relative transition-colors overflow-hidden bg-white duration-200 whitespace-nowrap active:scale-95",
        "text-stone-700 hover:text-stone-900 border-stone-200",
        "after:inset-0 after:absolute after:rounded-[inherit] after:bg-gradient-to-r after:from-transparent after:from-40% after:via-[var(--glow-color-via)] after:to-[var(--glow-color-to)] after:via-70% after:shadow-[rgba(0,0,0,0.05)_0px_1px_0px_inset] z-20",
        "before:absolute before:w-[6px] hover:before:translate-x-full before:transition-all before:duration-200 before:h-[60%] before:bg-[var(--glow-color)] before:right-0 before:rounded-l before:shadow-[-4px_0_15px_var(--glow-color)] z-10",
        className
      )}
    >
      {children}
    </button>
  );
}
