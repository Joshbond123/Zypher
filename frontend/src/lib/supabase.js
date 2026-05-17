import { createClient } from '@supabase/supabase-js'

const SUPABASE_URL = import.meta.env.VITE_SUPABASE_URL || 'https://beglgkjaejuvhqhddqfh.supabase.co'
const SUPABASE_ANON = import.meta.env.VITE_SUPABASE_ANON_KEY || 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJlZ2xna2phZWp1dmhxaGRkcWZoIiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzg1Nzc4MjcsImV4cCI6MjA5NDE1MzgyN30.lMYsJ6LdQlWDF4GvzimoXkJqhR8vg7A5zdNgtpLAm3Y'

export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON)
