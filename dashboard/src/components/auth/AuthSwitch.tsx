/**
 * The link under a sign-in form that switches between email sign-in and an API key.
 *
 * It sits in the page flow, under the form. It used to be fixed to the corner of
 * the viewport, where on a phone it covered the Sign in / Create account button
 * while the form scrolled under it.
 */
export function AuthSwitch({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <p className="mt-4 text-center">
      <button
        type="button"
        onClick={onClick}
        className="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 underline"
      >
        {label}
      </button>
    </p>
  );
}
