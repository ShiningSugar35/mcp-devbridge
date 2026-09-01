export interface SendConfirmationInput {
  beforeConversationRef: string | null;
  afterConversationRef: string | null;
  generationStarted: boolean;
  assistantTurnStarted: boolean;
}

export function isSendConfirmationSafe(input: SendConfirmationInput): boolean {
  if (input.beforeConversationRef === null) {
    return input.afterConversationRef !== null;
  }
  if (input.afterConversationRef !== input.beforeConversationRef) {
    return false;
  }
  return input.generationStarted || input.assistantTurnStarted;
}
