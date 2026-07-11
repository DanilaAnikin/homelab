import { Table } from "drizzle-orm";
import { createInsertSchema, createSelectSchema } from "drizzle-zod";

export function createTableSchemas<T extends Table>(table: T) {
  return {
    insert: createInsertSchema(table),
    select: createSelectSchema(table),
  };
}
