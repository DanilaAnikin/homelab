"use client"

import * as React from "react"
import { Slot } from "radix-ui"

import { cn } from "@workspace/ui/lib/utils"
import { Label } from "@workspace/ui/components/label"
import { FormMessage } from "@workspace/ui/components/form-message"

type FormFieldContextValue = {
  id: string
  descriptionId: string
  messageId: string
  invalid: boolean
}

const FormFieldContext = React.createContext<FormFieldContextValue | null>(null)

function useFormField() {
  const context = React.useContext(FormFieldContext)
  if (!context) {
    throw new Error("useFormField must be used within a <FormField>.")
  }
  return context
}

/**
 * Groups a Label, control, optional description, and an inline error message.
 * Pass `error` to mark the field invalid: the control receives `aria-invalid`
 * and `aria-describedby`, and `FormMessage` renders the error text.
 */
function FormField({
  className,
  error,
  children,
  id: idProp,
  ...props
}: React.ComponentProps<"div"> & { error?: React.ReactNode }) {
  const generatedId = React.useId()
  const id = idProp ?? generatedId
  const invalid = Boolean(error)

  const value = React.useMemo<FormFieldContextValue>(
    () => ({
      id,
      descriptionId: `${id}-description`,
      messageId: `${id}-message`,
      invalid,
    }),
    [id, invalid]
  )

  return (
    <FormFieldContext.Provider value={value}>
      <div
        data-slot="form-field"
        data-invalid={invalid || undefined}
        className={cn("group/field flex flex-col gap-2", className)}
        {...props}
      >
        {children}
        {error ? <FormMessage id={value.messageId}>{error}</FormMessage> : null}
      </div>
    </FormFieldContext.Provider>
  )
}

function FormLabel({
  className,
  ...props
}: React.ComponentProps<typeof Label>) {
  const { id, invalid } = useFormField()
  return (
    <Label
      data-slot="form-label"
      data-invalid={invalid || undefined}
      htmlFor={id}
      className={cn("data-[invalid]:text-destructive", className)}
      {...props}
    />
  )
}

/**
 * Wraps the actual input/select/textarea, wiring `id`, `aria-invalid`, and
 * `aria-describedby` onto the child via Slot so error messages are announced.
 */
function FormControl({ ...props }: React.ComponentProps<typeof Slot.Root>) {
  const { id, descriptionId, messageId, invalid } = useFormField()
  return (
    <Slot.Root
      data-slot="form-control"
      id={id}
      aria-invalid={invalid || undefined}
      aria-describedby={
        invalid ? `${descriptionId} ${messageId}` : descriptionId
      }
      {...props}
    />
  )
}

function FormDescription({
  className,
  ...props
}: React.ComponentProps<"p">) {
  const { descriptionId } = useFormField()
  return (
    <p
      data-slot="form-description"
      id={descriptionId}
      className={cn("text-caption text-muted-foreground", className)}
      {...props}
    />
  )
}

export {
  FormField,
  FormLabel,
  FormControl,
  FormDescription,
  useFormField,
}
