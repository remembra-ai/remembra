import { useEffect, useState } from 'react';
import { api } from '../lib/api';
import type { PlanInfoResponse, UsageResponse, DailyUsageResponse } from '../lib/api';
import { 
  CreditCard, 
  Zap, 
  Database, 
  Key, 
  Clock, 
  Check, 
  ExternalLink,
  Loader2,
  TrendingUp,
  AlertCircle,
  Crown
} from 'lucide-react';
import clsx from 'clsx';

interface PlanFeature {
  name: string;
  free: string | boolean;
  pro: string | boolean;
  team: string | boolean;
  enterprise: string | boolean;
}

const PLAN_FEATURES: PlanFeature[] = [
  { name: 'Memories', free: '50,000', pro: '500,000', team: '2,000,000', enterprise: 'Unlimited' },
  { name: 'API Calls/month', free: '100,000', pro: '1,000,000', team: '5,000,000', enterprise: '50M+' },
  { name: 'API Keys', free: '3', pro: '10', team: '50', enterprise: '100' },
  { name: 'Projects', free: '1', pro: '5', team: '100', enterprise: '1,000' },
  { name: 'Team Members', free: '1', pro: '5', team: '25', enterprise: '1,000' },
  { name: 'Webhooks', free: false, pro: true, team: true, enterprise: true },
  { name: 'Observability', free: false, pro: true, team: true, enterprise: true },
  { name: 'SSO/SAML', free: false, pro: false, team: false, enterprise: true },
  { name: 'Priority Support', free: false, pro: false, team: true, enterprise: true },
  { name: 'SLA', free: false, pro: false, team: false, enterprise: true },
];

function UsageBar({ 
  label, 
  current, 
  limit, 
  icon: Icon 
}: { 
  label: string; 
  current: number; 
  limit: number; 
  icon: React.ElementType;
}) {
  const percentage = limit > 0 ? Math.min((current / limit) * 100, 100) : 0;
  const isWarning = percentage >= 80;
  const isCritical = percentage >= 95;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Icon className={clsx(
            'w-4 h-4',
            isCritical ? 'text-red-500' : isWarning ? 'text-amber-500' : 'text-gray-500 dark:text-gray-400'
          )} />
          <span className="text-sm font-medium text-gray-700 dark:text-gray-300">{label}</span>
        </div>
        <span className="text-sm text-gray-500 dark:text-gray-400">
          {current.toLocaleString()} / {limit.toLocaleString()}
        </span>
      </div>
      <div className="h-2 bg-gray-100 dark:bg-gray-700 rounded-full overflow-hidden">
        <div 
          className={clsx(
            'h-full rounded-full transition-all duration-300',
            isCritical ? 'bg-red-500' : isWarning ? 'bg-amber-500' : 'bg-[#8B5CF6]'
          )}
          style={{ width: `${percentage}%` }}
        />
      </div>
      {isCritical && (
        <p className="text-xs text-red-600 dark:text-red-400 flex items-center gap-1">
          <AlertCircle className="w-3 h-3" />
          Approaching limit! Consider upgrading.
        </p>
      )}
    </div>
  );
}

function PricingCard({ 
  name, 
  price, 
  description, 
  features, 
  isCurrentPlan, 
  onUpgrade,
  isPopular,
  loading
}: {
  name: string;
  price: string;
  description: string;
  features: string[];
  isCurrentPlan: boolean;
  onUpgrade?: () => void;
  isPopular?: boolean;
  loading?: boolean;
}) {
  return (
    <div className={clsx(
      'relative p-6 rounded-xl border-2 transition-all',
      isPopular 
        ? 'border-[#8B5CF6] bg-blue-50/50 dark:bg-blue-900/10' 
        : 'border-gray-200 dark:border-gray-700',
      isCurrentPlan && 'ring-2 ring-green-500 ring-offset-2 dark:ring-offset-gray-900'
    )}>
      {isPopular && (
        <div className="absolute -top-3 left-1/2 -translate-x-1/2">
          <span className="bg-[#8B5CF6] text-white text-xs font-semibold px-3 py-1 rounded-full">
            Most Popular
          </span>
        </div>
      )}
      
      {isCurrentPlan && (
        <div className="absolute -top-3 right-4">
          <span className="bg-green-500 text-white text-xs font-semibold px-3 py-1 rounded-full flex items-center gap-1">
            <Check className="w-3 h-3" /> Current Plan
          </span>
        </div>
      )}

      <div className="text-center mb-6">
        <h3 className="text-xl font-bold text-gray-900 dark:text-white">{name}</h3>
        <div className="mt-2">
          <span className="text-4xl font-bold text-gray-900 dark:text-white">{price}</span>
          {price !== 'Custom' && price !== 'Free' && (
            <span className="text-gray-500 dark:text-gray-400">/month</span>
          )}
        </div>
        <p className="mt-2 text-sm text-gray-600 dark:text-gray-400">{description}</p>
      </div>

      <ul className="space-y-3 mb-6">
        {features.map((feature, i) => (
          <li key={i} className="flex items-start gap-2">
            <Check className="w-5 h-5 text-green-500 shrink-0 mt-0.5" />
            <span className="text-sm text-gray-700 dark:text-gray-300">{feature}</span>
          </li>
        ))}
      </ul>

      {onUpgrade && !isCurrentPlan && (
        <button
          onClick={onUpgrade}
          disabled={loading}
          className={clsx(
            'w-full py-3 px-4 rounded-lg font-semibold transition-all flex items-center justify-center gap-2',
            isPopular
              ? 'bg-[#8B5CF6] hover:bg-[#7C3AED] text-white'
              : 'bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-900 dark:text-white',
            loading && 'opacity-50 cursor-not-allowed'
          )}
        >
          {loading ? (
            <Loader2 className="w-5 h-5 animate-spin" />
          ) : (
            <>
              <Zap className="w-5 h-5" />
              Upgrade to {name}
            </>
          )}
        </button>
      )}

      {name === 'Enterprise' && (
        <a
          href="mailto:sales@dolphytech.com?subject=Remembra Enterprise Inquiry"
          className="w-full py-3 px-4 rounded-lg font-semibold transition-all flex items-center justify-center gap-2 bg-gray-900 dark:bg-white hover:bg-gray-800 dark:hover:bg-gray-100 text-white dark:text-gray-900"
        >
          Contact Sales
          <ExternalLink className="w-4 h-4" />
        </a>
      )}
    </div>
  );
}

export function Billing() {
  const [planInfo, setPlanInfo] = useState<PlanInfoResponse | null>(null);
  const [usage, setUsage] = useState<UsageResponse | null>(null);
  const [_dailyUsage, setDailyUsage] = useState<DailyUsageResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [checkoutLoading, setCheckoutLoading] = useState(false);
  const [portalLoading, setPortalLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadBillingData();
  }, []);

  const loadBillingData = async () => {
    setLoading(true);
    setError(null);
    try {
      const [planData, usageData, dailyData] = await Promise.all([
        api.getPlanInfo().catch(() => null),
        api.getUsage().catch(() => null),
        api.getDailyUsage(7).catch(() => null),
      ]);
      setPlanInfo(planData);
      setUsage(usageData);
      setDailyUsage(dailyData);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load billing data');
    } finally {
      setLoading(false);
    }
  };

  const handleUpgrade = async (plan: string) => {
    setCheckoutLoading(true);
    try {
      const { checkout_url } = await api.createCheckout(plan);
      window.location.href = checkout_url;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create checkout session');
    } finally {
      setCheckoutLoading(false);
    }
  };

  const handleManageSubscription = async () => {
    setPortalLoading(true);
    try {
      const { portal_url } = await api.createPortalSession();
      window.location.href = portal_url;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to open billing portal');
    } finally {
      setPortalLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="w-8 h-8 animate-spin text-[#8B5CF6]" />
      </div>
    );
  }

  const currentPlan = planInfo?.plan || 'free';
  const isPro = currentPlan === 'pro';
  const isTeam = currentPlan === 'team';
  const isEnterprise = currentPlan === 'enterprise';
  const hasPaidPlan = isPro || isTeam || isEnterprise;

  return (
    <div className="space-y-8">
      {error && (
        <div className="p-4 rounded-lg bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-400 flex items-center gap-2">
          <AlertCircle className="w-5 h-5" />
          {error}
        </div>
      )}

      {/* Current Plan Banner */}
      <div className={clsx(
        'p-6 rounded-xl border-2',
        hasPaidPlan
          ? 'border-[#8B5CF6] bg-gradient-to-r from-blue-50 to-purple-50 dark:from-blue-900/20 dark:to-purple-900/20'
          : 'border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800'
      )}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            {hasPaidPlan ? (
              <div className="p-3 rounded-full bg-gradient-to-r from-#8B5CF6 to-purple-500">
                <Crown className="w-6 h-6 text-white" />
              </div>
            ) : (
              <div className="p-3 rounded-full bg-gray-200 dark:bg-gray-700">
                <CreditCard className="w-6 h-6 text-gray-600 dark:text-gray-400" />
              </div>
            )}
            <div>
              <h2 className="text-xl font-bold text-gray-900 dark:text-white capitalize">
                {currentPlan} Plan
              </h2>
              <p className="text-sm text-gray-600 dark:text-gray-400">
                {isPro 
                  ? 'Thank you for being a Pro subscriber!' 
                  : isTeam
                    ? 'Thank you for being a Team subscriber!'
                    : isEnterprise 
                      ? 'Enterprise features enabled'
                      : 'Upgrade to unlock more features'}
              </p>
            </div>
          </div>
          
          {hasPaidPlan && (
            <button
              onClick={handleManageSubscription}
              disabled={portalLoading}
              className="px-4 py-2 rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors flex items-center gap-2"
            >
              {portalLoading ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <>
                  <CreditCard className="w-4 h-4" />
                  Manage Subscription
                </>
              )}
            </button>
          )}
        </div>
      </div>

      {/* Usage Meters */}
      {planInfo && (
        <div className="p-6 rounded-xl border border-gray-200 dark:border-gray-700">
          <div className="flex items-center gap-2 mb-6">
            <TrendingUp className="w-5 h-5 text-[#8B5CF6]" />
            <h3 className="text-lg font-semibold text-gray-900 dark:text-white">Usage This Month</h3>
            {usage?.period && (
              <span className="text-sm text-gray-500 dark:text-gray-400">({usage.period})</span>
            )}
          </div>

          <div className="grid gap-6 md:grid-cols-2">
            <UsageBar
              label="Memories Stored"
              current={planInfo.usage.memories_stored || 0}
              limit={(planInfo.limits.max_memories as number) || 10000}
              icon={Database}
            />
            <UsageBar
              label="API Calls (Stores)"
              current={planInfo.usage.stores_this_month || 0}
              limit={(planInfo.limits.max_stores_per_month as number) || 10000}
              icon={Zap}
            />
            <UsageBar
              label="API Calls (Recalls)"
              current={planInfo.usage.recalls_this_month || 0}
              limit={(planInfo.limits.max_recalls_per_month as number) || 50000}
              icon={Clock}
            />
            <UsageBar
              label="Active API Keys"
              current={planInfo.usage.api_keys_active || 0}
              limit={(planInfo.limits.max_api_keys as number) || 3}
              icon={Key}
            />
          </div>
        </div>
      )}

      {/* Pricing Cards */}
      {!isEnterprise && (
        <div>
          <h3 className="text-lg font-semibold text-gray-900 dark:text-white mb-6">
            {hasPaidPlan ? 'Your Plan' : 'Choose Your Plan'}
          </h3>
          
          <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-4">
            <PricingCard
              name="Free"
              price="Free"
              description="For indie devs and testing"
              features={[
                '50,000 memories',
                '100K API calls/month',
                '3 API keys',
                '1 project',
                'Hybrid search',
                'Entity resolution',
              ]}
              isCurrentPlan={currentPlan === 'free'}
            />
            
            <PricingCard
              name="Pro"
              price="$49"
              description="For startups & side projects"
              features={[
                '500,000 memories',
                '1M API calls/month',
                '10 API keys',
                '5 projects',
                '5 team members',
                'Webhooks & observability',
              ]}
              isCurrentPlan={isPro}
              isPopular={true}
              onUpgrade={() => handleUpgrade('pro')}
              loading={checkoutLoading}
            />
            
            <PricingCard
              name="Team"
              price="$99"
              description="For growing companies"
              features={[
                '2,000,000 memories',
                '5M API calls/month',
                '50 API keys',
                '100 projects',
                '25 team members',
                'Priority support',
              ]}
              isCurrentPlan={isTeam}
              onUpgrade={() => handleUpgrade('team')}
              loading={checkoutLoading}
            />
            
            <PricingCard
              name="Enterprise"
              price="Custom"
              description="For large-scale deployments"
              features={[
                'Unlimited memories',
                '50M+ API calls/month',
                '100 API keys',
                'SSO/SAML',
                '1,000 team members',
                'Dedicated SLA',
              ]}
              isCurrentPlan={isEnterprise}
            />
          </div>
        </div>
      )}

      {/* Feature Comparison Table */}
      <div className="overflow-hidden rounded-xl border border-gray-200 dark:border-gray-700">
        <table className="w-full">
          <thead className="bg-gray-50 dark:bg-gray-800">
            <tr>
              <th className="px-4 py-4 text-left text-sm font-semibold text-gray-900 dark:text-white">
                Feature
              </th>
              <th className="px-4 py-4 text-center text-sm font-semibold text-gray-900 dark:text-white">
                Free
              </th>
              <th className="px-4 py-4 text-center text-sm font-semibold text-[#8B5CF6] dark:text-[#A78BFA]">
                Pro $49
              </th>
              <th className="px-4 py-4 text-center text-sm font-semibold text-purple-600 dark:text-purple-400">
                Team $99
              </th>
              <th className="px-4 py-4 text-center text-sm font-semibold text-gray-900 dark:text-white">
                Enterprise
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
            {PLAN_FEATURES.map((feature, i) => (
              <tr key={i} className={i % 2 === 0 ? 'bg-white dark:bg-gray-900' : 'bg-gray-50 dark:bg-gray-800/50'}>
                <td className="px-4 py-3 text-sm text-gray-700 dark:text-gray-300">
                  {feature.name}
                </td>
                <td className="px-4 py-3 text-center text-sm">
                  {typeof feature.free === 'boolean' ? (
                    feature.free ? (
                      <Check className="w-5 h-5 text-green-500 mx-auto" />
                    ) : (
                      <span className="text-gray-400">—</span>
                    )
                  ) : (
                    <span className="text-gray-700 dark:text-gray-300">{feature.free}</span>
                  )}
                </td>
                <td className="px-4 py-3 text-center text-sm bg-blue-50/50 dark:bg-blue-900/10">
                  {typeof feature.pro === 'boolean' ? (
                    feature.pro ? (
                      <Check className="w-5 h-5 text-green-500 mx-auto" />
                    ) : (
                      <span className="text-gray-400">—</span>
                    )
                  ) : (
                    <span className="font-medium text-#7C3AED dark:text-[#A78BFA]">{feature.pro}</span>
                  )}
                </td>
                <td className="px-4 py-3 text-center text-sm bg-purple-50/50 dark:bg-purple-900/10">
                  {typeof feature.team === 'boolean' ? (
                    feature.team ? (
                      <Check className="w-5 h-5 text-green-500 mx-auto" />
                    ) : (
                      <span className="text-gray-400">—</span>
                    )
                  ) : (
                    <span className="font-medium text-purple-700 dark:text-purple-400">{feature.team}</span>
                  )}
                </td>
                <td className="px-4 py-3 text-center text-sm">
                  {typeof feature.enterprise === 'boolean' ? (
                    feature.enterprise ? (
                      <Check className="w-5 h-5 text-green-500 mx-auto" />
                    ) : (
                      <span className="text-gray-400">—</span>
                    )
                  ) : (
                    <span className="text-gray-700 dark:text-gray-300">{feature.enterprise}</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* FAQ or Help */}
      <div className="p-6 rounded-xl bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700">
        <h3 className="text-lg font-semibold text-gray-900 dark:text-white mb-4">
          Frequently Asked Questions
        </h3>
        <div className="space-y-4">
          <div>
            <h4 className="font-medium text-gray-900 dark:text-white">What happens when I hit my limits?</h4>
            <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">
              You'll receive a warning at 80% usage. API calls beyond your limit will return a 429 error until the next billing cycle or you upgrade.
            </p>
          </div>
          <div>
            <h4 className="font-medium text-gray-900 dark:text-white">Can I downgrade my plan?</h4>
            <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">
              Yes, you can downgrade at any time through the billing portal. Changes take effect at the end of your current billing period.
            </p>
          </div>
          <div>
            <h4 className="font-medium text-gray-900 dark:text-white">Is there a free trial?</h4>
            <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">
              The Free tier is unlimited in time! You can use it forever for personal projects. Upgrade when you need more capacity.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
