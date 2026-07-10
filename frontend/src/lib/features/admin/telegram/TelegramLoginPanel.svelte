<script lang="ts">
  import AdvancedSection from '$lib/features/admin/AdvancedSection.svelte';
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import { Button, FormRow, Input } from '$lib/ui';

  let { onStartQrLogin }: { onStartQrLogin?: (event: SubmitEvent) => void } = $props();
</script>

<AdminPanel title="Connect a Telegram account">
  <div class="grid gap-4">
    <div class="grid gap-2">
      <p class="m-0 text-sm text-muted">Connect an account to fetch Telegram sources. QR is the quickest option and does not require entering a phone number here.</p>
      <form method="POST" action="?/startQrLogin" onsubmit={onStartQrLogin} class="flex flex-wrap gap-2">
        <Button type="submit">Connect with QR</Button>
      </form>
    </div>

    <AdvancedSection title="Use phone instead" description="Enter a phone number only when QR sign-in is not available. Telegram will ask for its verification code next.">
      <form method="POST" action="?/startPhoneLogin" class="grid max-w-xl gap-3">
        <FormRow label="Phone number">
          <Input name="phone_number" autocomplete="tel" inputmode="tel" placeholder="Enter full phone number" required />
        </FormRow>
        <Button type="submit" variant="secondary">Continue with phone</Button>
      </form>
    </AdvancedSection>
  </div>
</AdminPanel>
