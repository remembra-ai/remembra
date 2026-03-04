import { Database, Users, HardDrive, Activity, TrendingUp, TrendingDown } from 'lucide-react';
import clsx from 'clsx';

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
}

function StatCard({ label, value, subtext, icon: Icon, trend, color = 'purple' }: StatCardProps) {
  const colorStyles = {
    purple: {
      iconBg: 'bg-[#8B5CF6]/10',
      iconColor: 'text-[#8B5CF6]',
      trendPositive: 'text-green-400',
      trendNegative: 'text-red-400',
    },
    blue: {
      iconBg: 'bg-blue-500/10',
      iconColor: 'text-blue-400',
      trendPositive: 'text-green-400',
      trendNegative: 'text-red-400',
    },
    green: {
      iconBg: 'bg-green-500/10',
      iconColor: 'text-green-400',
      trendPositive: 'text-green-400',
      trendNegative: 'text-red-400',
    },
    amber: {
      iconBg: 'bg-amber-500/10',
      iconColor: 'text-amber-400',
      trendPositive: 'text-green-400',
      trendNegative: 'text-red-400',
    },
  };

  const styles = colorStyles[color];

  return (
    <div className={clsx(
      'group relative p-5 rounded-xl',
      'bg-[hsl(var(--card))] border border-[hsl(var(--border))]',
      'hover:border-[#8B5CF6]/30 hover:shadow-lg hover:shadow-purple-500/5',
      'transition-all duration-200'
    )}>
      {/* Subtle gradient overlay on hover */}
      <div className="absolute inset-0 rounded-xl bg-gradient-to-br from-[#8B5CF6]/5 to-transparent opacity-0 group-hover:opacity-100 transition-opacity" />
      
      <div className="relative flex items-start justify-between">
        <div>
          <p className="text-sm text-[hsl(var(--muted-foreground))] mb-1">{label}</p>
          <p className="text-2xl font-bold text-[hsl(var(--foreground))]">{value}</p>
          {subtext && (
            <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1">{subtext}</p>
          )}
          {trend && (
            <div className={clsx(
              'flex items-center gap-1 mt-2 text-xs font-medium',
              trend.value >= 0 ? styles.trendPositive : styles.trendNegative
            )}>
              {trend.value >= 0 ? (
                <TrendingUp className="w-3 h-3" />
              ) : (
                <TrendingDown className="w-3 h-3" />
              )}
              <span>{trend.value >= 0 ? '+' : ''}{trend.value}%</span>
              <span className="text-[hsl(var(--muted-foreground))]">{trend.label}</span>
            </div>
          )}
        </div>
        
        <div className={clsx('p-2.5 rounded-lg', styles.iconBg)}>
          <Icon className={clsx('w-5 h-5', styles.iconColor)} />
        </div>
      </div>
    </div>
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
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        {[...Array(4)].map((_, i) => (
          <div 
            key={i}
            className="p-5 rounded-xl bg-[hsl(var(--card))] border border-[hsl(var(--border))] animate-pulse"
          >
            <div className="h-4 bg-[hsl(var(--muted))] rounded w-20 mb-3" />
            <div className="h-8 bg-[hsl(var(--muted))] rounded w-24 mb-2" />
            <div className="h-3 bg-[hsl(var(--muted))] rounded w-16" />
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
      <StatCard
        label="Total Memories"
        value={memoryCount.toLocaleString()}
        icon={Database}
        trend={{ value: 12, label: 'this week' }}
        color="purple"
      />
      <StatCard
        label="Entities"
        value={entityCount.toLocaleString()}
        subtext={`${Math.round(memoryCount / Math.max(entityCount, 1))} memories/entity avg`}
        icon={Users}
        color="blue"
      />
      <StatCard
        label="Storage Used"
        value={storageUsed}
        subtext="of 5 GB"
        icon={HardDrive}
        color="green"
      />
      <StatCard
        label="API Calls"
        value={apiCalls.toLocaleString()}
        subtext="this month"
        icon={Activity}
        trend={{ value: 8, label: 'vs last month' }}
        color="amber"
      />
    </div>
  );
}
