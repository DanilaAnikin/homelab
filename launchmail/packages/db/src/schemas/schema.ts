import * as pg from "drizzle-orm/pg-core";

export const todosTable = pg.pgTable("todos", {
  id: pg.uuid("id").primaryKey().defaultRandom(),
  title: pg.text("title").notNull(),
  completed: pg.boolean("completed").notNull().default(false),
  created_at: pg.timestamp().defaultNow(),
});
