import { Database, Users, HardDrive, Activity, TrendingUp, TrendingDown } from 'lucide-react';
import { motion, useMotionValue, useTransform, animate } from 'framer-motion';
import { useEffect } from 'react';
import clsx from 'clsx';

function AnimatedNumber({ value }: { value: string | number }) {
  const count = useMotionValue(0);
  const rounded = useTransform(count, (latest) => Math.round(latest).toLocaleString());
  const numericValue = typeof value === 'number' ? value : 0;

  useEffect(() => {
    if (typeof value === 'string') return;
    const animation = animate(count, numericValue, { duration: 1.5, ease: "easeOut" });
    return animation.stop;
  }, [value, numericValue, count]);

  if (typeof value === 'string') {
    return <span>{value}</span>;
  }

  return <motion.span>{rounded}</motion.span>;
}

interface StatCardProps {
  label: string;
  value: string | number;
  subtext?: string;
  icon: React.ElementType;
  trend?: {
    value: number;
    label: string;
  };
  color?: 'purple' | 'blue' | 'green' | 'amber';
  delay?: number;
}

function StatCard({ label, value, subtext, icon: Icon, trend, color = 'purple', delay = 0 }: StatCardProps) {
  const colorStyles = {
    purple: {
      accent: '#FF5B14',
      iconBg: 'rgba(255, 91, 20, 0.12)',
      iconBorder: 'rgba(255, 91, 20, 0.22)',
      glow: 'rgba(255, 91, 20, 0.16)',
    },
    blue: {
      accent: '#60A5FA',
      iconBg: 'rgba(96, 165, 250, 0.12)',
      iconBorder: 'rgba(96, 165, 250, 0.22)',
      glow: 'rgba(96, 165, 250, 0.16)',
    },
    green: {
      accent: '#34D399',
      iconBg: 'rgba(52, 211, 153, 0.12)',
      iconBorder: 'rgba(52, 211, 153, 0.22)',
      glow: 'rgba(52, 211, 153, 0.16)',
    },
    amber: {
      accent: '#F59E0B',
      iconBg: 'rgba(245, 158, 11, 0.12)',
      iconBorder: 'rgba(245, 158, 11, 0.22)',
      glow: 'rgba(245, 158, 11, 0.16)',
    },
  };

  const styles = colorStyles[color];

  return (
    <motion.div 
      initial={{ opacity: 0, y: 15 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay, ease: "easeOut" }}
      className={clsx(
        'group dashboard-surface relative overflow-hidden rounded-[4px] p-6',
        'hover:-translate-y-0.5',
        'transition-all duration-300'
      )}
      style={{
        boxShadow: `0 24px 60px hsl(0 0% 0% / 0.14), inset 0 1px 0 hsl(0 0% 100% / 0.05), 0 0 0 1px ${styles.glow}`,
      }}
    >
      {/* Subtle glowing animated background gradient */}
      <div
        className="absolute inset-0 opacity-80 transition-opacity duration-500 group-hover:opacity-100"
        style={{
          background: `radial-gradient(circle at 100% 0%, ${styles.glow} 0%, transparent 45%)`,
        }}
      />
      
      <div className="relative flex items-start justify-between z-10">
        <div>
          <p className="mb-1.5 text-[12px] font-semibold uppercase tracking-[0.24em] text-[hsl(var(--muted-foreground))]">{label}</p>
          <p className="text-3xl font-bold tracking-tight text-[hsl(var(--foreground))]">
            <AnimatedNumber value={value} />
          </p>
          {subtext && (
            <p className="mt-1 text-[13px] font-medium text-[hsl(var(--muted-foreground))]">{subtext}</p>
          )}
          {trend && (
            <div
              className={clsx(
                'mt-3 flex w-fit items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold',
                trend.value >= 0 ? 'text-emerald-500 dark:text-emerald-400' : 'text-red-500 dark:text-red-400'
              )}
              style={{
                backgroundColor: 'hsl(var(--muted) / 0.7)',
                border: '1px solid hsl(var(--border) / 0.72)',
              }}
            >
              {trend.value >= 0 ? (
                <TrendingUp className="w-3.5 h-3.5" />
              ) : (
                <TrendingDown className="w-3.5 h-3.5" />
              )}
              <span>{trend.value >= 0 ? '+' : ''}{trend.value}%</span>
              <span className="ml-0.5 font-medium text-[hsl(var(--muted-foreground))]">{trend.label}</span>
            </div>
          )}
        </div>
        
        <div
          className="rounded-2xl p-3 shadow-inner"
          style={{
            backgroundColor: styles.iconBg,
            border: `1px solid ${styles.iconBorder}`,
            boxShadow: `inset 0 1px 0 rgba(255,255,255,0.05), 0 10px 24px ${styles.glow}`,
          }}
        >
          <Icon className="w-6 h-6" style={{ color: styles.accent }} />
        </div>
      </div>
    </motion.div>
  );
}

interface StatsOverviewProps {
  memoryCount: number;
  entityCount: number;
  storageUsed: string;
  apiCalls: number;
  loading?: boolean;
}

export function StatsOverview({ 
  memoryCount, 
  entityCount, 
  storageUsed, 
  apiCalls,
  loading 
}: StatsOverviewProps) {
  if (loading) {
    return (
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 md:gap-6 mb-8">
        {[...Array(4)].map((_, i) => (
          <div 
            key={i}
            className="dashboard-surface h-36 animate-pulse rounded-[4px] p-6"
          >
            <div className="mb-4 h-4 w-24 rounded bg-[hsl(var(--muted))]" />
            <div className="mb-3 h-8 w-28 rounded bg-[hsl(var(--muted))]" />
            <div className="h-3 w-16 rounded bg-[hsl(var(--muted))]" />
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 md:gap-6 mb-8">
      <StatCard
        label="Total Memories"
        value={memoryCount}
        icon={Database}
        trend={{ value: 12, label: 'this week' }}
        color="purple"
        delay={0.1}
      />
      <StatCard
        label="Entities Count"
        value={entityCount}
        subtext={`${Math.round(memoryCount / Math.max(entityCount, 1))} memories/entity avg`}
        icon={Users}
        color="blue"
        delay={0.2}
      />
      <StatCard
        label="Storage Used"
        value={storageUsed}
        subtext="of 500 MB capacity"
        icon={HardDrive}
        color="green"
        delay={0.3}
      />
      <StatCard
        label="API Operations"
        value={apiCalls}
        subtext="past 30 days"
        icon={Activity}
        trend={{ value: 8, label: 'vs last month' }}
        color="amber"
        delay={0.4}
      />
    </div>
  );
}
