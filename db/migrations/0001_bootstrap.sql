-- Phase DB1 cluster bootstrap marker.
-- market_db.migrate applies the role and CREATE DATABASE operations because
-- PostgreSQL does not allow CREATE DATABASE inside a transaction block.
-- This file is still checksummed and recorded in every target database.
SELECT 1;
