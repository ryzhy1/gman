import { cn } from '@/lib/utils';
import { badgeVariants } from './badge';
import { buttonVariants } from './button';
import { Progress } from './progress';

/** Status tones used across the portal: '' — fine, warn, bad, info. */
const VARIANTS: Record<string, 'success' | 'warning' | 'danger' | 'muted'> = {
  '': 'success', ok: 'success', warn: 'warning', bad: 'danger', critical: 'danger', info: 'muted',
};

/** shadcn badge classes for a status tone; for spans and buttons that keep their own markup. */
export const tone = (t = '', className?: string) => cn(badgeVariants({ variant: VARIANTS[t] ?? 'muted' }), className);

/** Secondary action ("Показать еще", "Отмена"). */
export const outlineButton = (className?: string) => cn(buttonVariants({ variant: 'outline', size: 'sm' }), className);
export const linkButton = (className?: string) =>
  cn(buttonVariants({ variant: 'link', size: 'sm' }), 'h-auto p-0 font-semibold', className);

/** Ratio meter over shadcn Progress; `hot` marks an exceeded limit. */
export function Meter({ value, max = 1, hot, className }: { value: number; max?: number | string; hot?: boolean; className?: string }) {
  const total = Number(max) || 1;
  return <Progress value={Math.max(0, Math.min(100, value / total * 100))}
    className={cn('my-1.5 h-1.5', hot && 'hot', className)} />;
}
