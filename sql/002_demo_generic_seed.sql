/* Seed data for the generic demo. See the specification's Appendix A.

   Deliberate awkwardness, so that serialization bugs show up in testing rather
   than in front of a customer:
     - accented and non-Latin names (Ana Sofía Ríos, 陳偉明, Zoë Ødegård)
     - a name containing a comma and one containing an apostrophe
     - a name containing an embedded line break
     - NULL optional fields, decimals, dates and timestamps
*/
USE GraphXRConnectorDemo;
GO
DELETE FROM dbo.AccountTransfer; DELETE FROM dbo.Ownership; DELETE FROM dbo.Employment;
DELETE FROM dbo.Account; DELETE FROM dbo.Organization; DELETE FROM dbo.Person;
GO

INSERT INTO dbo.Person (person_id, full_name, date_of_birth, country_code, risk_score, source_updated_at) VALUES
 (1001, N'Avery Chen',              '1981-04-02', 'US', 12.50, '2026-01-04T09:00:00.000'),
 (1002, N'Ana Sofía Ríos',          '1975-11-19', 'ES', 64.25, '2026-01-04T09:00:00.000'),
 (1003, N'陳偉明',                    '1990-07-30', 'HK', 88.00, '2026-01-04T09:00:00.000'),
 (1004, N'Zoë Ødegård',             '1988-02-11', 'NO',  5.75, '2026-01-04T09:00:00.000'),
 (1005, N'Smith, Jonathan',         '1969-09-05', 'GB', 41.00, '2026-01-04T09:00:00.000'),  -- a comma
 (1006, N'Siobhán O''Connor',        '1993-03-17', 'IE', 33.33, '2026-01-04T09:00:00.000'),  -- an apostrophe
 (1007, N'Line' + CHAR(13) + CHAR(10) + N'Break Test', NULL, 'US', NULL, '2026-01-04T09:00:00.000'), -- a line break
 (1008, N'Dmitri Volkov',           '1972-12-24', 'RU', 91.10, '2026-01-04T09:00:00.000'),
 (1009, N'Fatima Al-Sayed',         '1986-06-08', 'AE', 22.40, '2026-01-04T09:00:00.000'),
 (1010, N'Kwame Mensah',            NULL,         'GH', NULL,  '2026-01-04T09:00:00.000'),  -- NULLs
 (1011, N'Hana Suzuki',             '1997-01-23', 'JP', 17.85, '2026-01-04T09:00:00.000'),
 (1012, N'Miguel dos Santos',       '1983-08-14', 'BR', 55.00, '2026-01-04T09:00:00.000');

INSERT INTO dbo.Organization (organization_id, name, industry, country_code, source_updated_at) VALUES
 (2001, N'Northwind Logistics',        N'Transport',  'US', '2026-01-04T09:00:00.000'),
 (2002, N'Banco Ríos, S.A.',           N'Finance',    'ES', '2026-01-04T09:00:00.000'),
 (2003, N'Kowloon Trading Co.',        N'Wholesale',  'HK', '2026-01-04T09:00:00.000'),
 (2004, N'Fjord Marine AS',            NULL,          'NO', '2026-01-04T09:00:00.000'),
 (2005, N'Sahara Holdings FZE',        N'Investment', 'AE', '2026-01-04T09:00:00.000');

INSERT INTO dbo.Account (account_id, account_number, institution, country_code, opened_on, source_updated_at) VALUES
 (3001, N'GB29-NWBK-6016-1331-9268-19', N'NatWest',      'GB', '2019-05-01', '2026-01-04T09:00:00.000'),
 (3002, N'ES91-2100-0418-4502-0005-1332',N'CaixaBank',   'ES', '2020-02-14', '2026-01-04T09:00:00.000'),
 (3003, N'HK55-HSBC-0000-1234-5678',     N'HSBC',        'HK', '2018-11-30', '2026-01-04T09:00:00.000'),
 (3004, N'NO93-8601-1117-947',           N'DNB',         'NO', '2021-07-19', '2026-01-04T09:00:00.000'),
 (3005, N'AE07-0331-2345-6789-0123-456', N'Emirates NBD','AE', '2017-03-08', '2026-01-04T09:00:00.000'),
 (3006, N'US64-SVBK-XXXX-1111',          N'SVB',         'US', NULL,         '2026-01-04T09:00:00.000'),
 (3007, N'JP12-MUFG-0000-9999',          N'MUFG',        'JP', '2022-09-01', '2026-01-04T09:00:00.000'),
 (3008, N'BR15-ITAU-0000-4321',          N'Itaú',        'BR', '2016-12-12', '2026-01-04T09:00:00.000'),
 (3009, N'RU02-SBER-0000-7777',          NULL,           'RU', '2015-04-25', '2026-01-04T09:00:00.000'),
 (3010, N'GH88-GCBL-0000-2468',          N'GCB Bank',    'GH', '2023-01-05', '2026-01-04T09:00:00.000');

/* Employment. Person 1002 works for TWO organisations; organisation 2001 employs
   THREE people — both shapes the specification asks for. */
INSERT INTO dbo.Employment (employment_id, person_id, organization_id, job_title, start_date, end_date, source_updated_at) VALUES
 (9001, 1001, 2001, N'Director',            '2021-02-01', NULL,        '2026-01-04T09:00:00.000'),
 (9002, 1005, 2001, N'Logistics Manager',   '2019-06-15', NULL,        '2026-01-04T09:00:00.000'),
 (9003, 1007, 2001, N'Driver',              '2022-01-10', '2024-03-31','2026-01-04T09:00:00.000'),
 (9004, 1002, 2002, N'Compliance Lead',     '2018-09-03', NULL,        '2026-01-04T09:00:00.000'),
 (9005, 1002, 2005, N'Non-Executive Director','2023-04-01', NULL,      '2026-01-04T09:00:00.000'),
 (9006, 1003, 2003, N'Owner',               '2015-01-01', NULL,        '2026-01-04T09:00:00.000'),
 (9007, 1004, 2004, N'Skipper',             NULL,         NULL,        '2026-01-04T09:00:00.000'),
 (9008, 1008, 2005, N'Advisor',             '2020-10-20', NULL,        '2026-01-04T09:00:00.000'),
 (9009, 1009, 2005, N'Analyst',             '2024-02-05', NULL,        '2026-01-04T09:00:00.000');

/* Ownership. Account 3005 has THREE owners — the "one account, several owners" case. */
INSERT INTO dbo.Ownership (ownership_id, person_id, account_id, ownership_pct, source_updated_at) VALUES
 (7001, 1001, 3006, 100.00, '2026-01-04T09:00:00.000'),
 (7002, 1002, 3002,  50.00, '2026-01-04T09:00:00.000'),
 (7003, 1002, 3001,  25.50, '2026-01-04T09:00:00.000'),
 (7004, 1003, 3003, 100.00, '2026-01-04T09:00:00.000'),
 (7005, 1004, 3004,  75.25, '2026-01-04T09:00:00.000'),
 (7006, 1005, 3001,  74.50, '2026-01-04T09:00:00.000'),
 (7007, 1008, 3005,  40.00, '2026-01-04T09:00:00.000'),
 (7008, 1009, 3005,  35.00, '2026-01-04T09:00:00.000'),
 (7009, 1012, 3005,  25.00, '2026-01-04T09:00:00.000'),
 (7010, 1011, 3007, 100.00, '2026-01-04T09:00:00.000'),
 (7011, 1012, 3008, 100.00, '2026-01-04T09:00:00.000'),
 (7012, 1010, 3010,  NULL,  '2026-01-04T09:00:00.000');

/* Transfers. 3001 -> 3002 -> 3003 -> 3001 is a deliberate three-account cycle.
   Transfers 8021 and 8022 are a PARALLEL PAIR: same two accounts, twice. They must
   come back as two separate edges, which is why edge ids use the transfer's own key. */
INSERT INTO dbo.AccountTransfer (transfer_id, from_account_id, to_account_id, amount, currency_code, transferred_at, source_updated_at) VALUES
 (8001, 3001, 3002,   12500.0000, 'GBP', '2026-01-05T10:15:00.000', '2026-01-06T00:00:00.000'),
 (8002, 3002, 3003,   11800.5000, 'EUR', '2026-01-06T11:20:00.000', '2026-01-07T00:00:00.000'),
 (8003, 3003, 3001,   10990.7500, 'HKD', '2026-01-07T12:25:00.000', '2026-01-08T00:00:00.000'),
 (8004, 3004, 3005,     450.0000, 'NOK', '2026-01-08T08:00:00.000', '2026-01-09T00:00:00.000'),
 (8005, 3005, 3006,  1000000.0000,'AED', '2026-01-09T09:30:00.000', '2026-01-10T00:00:00.000'),
 (8006, 3006, 3007,    2750.2500, 'USD', '2026-01-10T14:45:00.000', '2026-01-11T00:00:00.000'),
 (8007, 3007, 3008,     999.9900, 'JPY', '2026-01-11T15:00:00.000', '2026-01-12T00:00:00.000'),
 (8008, 3008, 3009,    5600.0000, 'BRL', '2026-01-12T16:10:00.000', '2026-01-13T00:00:00.000'),
 (8009, 3009, 3010,     320.4500, 'RUB', '2026-01-13T17:20:00.000', '2026-01-14T00:00:00.000'),
 (8010, 3010, 3001,      75.0000, 'GHS', '2026-01-14T18:30:00.000', '2026-01-15T00:00:00.000'),
 (8011, 3001, 3003,    8400.0000, 'GBP', '2026-01-15T09:05:00.000', '2026-01-16T00:00:00.000'),
 (8012, 3002, 3005,    6200.0000, 'EUR', '2026-01-16T10:15:00.000', '2026-01-17T00:00:00.000'),
 (8013, 3003, 3006,    4100.0000, 'HKD', '2026-01-17T11:25:00.000', '2026-01-18T00:00:00.000'),
 (8014, 3004, 3007,    3050.0000, 'NOK', '2026-01-18T12:35:00.000', '2026-01-19T00:00:00.000'),
 (8015, 3005, 3008,    2900.0000, 'AED', '2026-01-19T13:45:00.000', '2026-01-20T00:00:00.000'),
 (8016, 3006, 3009,    1850.0000, 'USD', '2026-01-20T14:55:00.000', '2026-01-21T00:00:00.000'),
 (8017, 3007, 3010,     760.0000, 'JPY', '2026-01-21T15:05:00.000', '2026-01-22T00:00:00.000'),
 (8018, 3008, 3001,     640.0000, 'BRL', '2026-01-22T16:15:00.000', '2026-01-23T00:00:00.000'),
 (8019, 3009, 3002,     530.0000, 'RUB', '2026-01-23T17:25:00.000', '2026-01-24T00:00:00.000'),
 (8020, 3010, 3003,     410.0000, 'GHS', '2026-01-24T18:35:00.000', '2026-01-25T00:00:00.000'),
 (8021, 3001, 3005,    9100.0000, 'GBP', '2026-01-25T08:00:00.000', '2026-01-26T00:00:00.000'),
 (8022, 3001, 3005,    9100.0000, 'GBP', '2026-01-25T08:05:00.000', '2026-01-26T00:00:00.000'),
 (8023, 3005, 3005,     100.0000, 'AED', '2026-01-26T08:10:00.000', '2026-01-27T00:00:00.000'); -- a self-transfer
GO

SELECT 'Person' AS [table], COUNT(*) AS n FROM dbo.Person
UNION ALL SELECT 'Organization', COUNT(*) FROM dbo.Organization
UNION ALL SELECT 'Account',      COUNT(*) FROM dbo.Account
UNION ALL SELECT 'Employment',   COUNT(*) FROM dbo.Employment
UNION ALL SELECT 'Ownership',    COUNT(*) FROM dbo.Ownership
UNION ALL SELECT 'AccountTransfer', COUNT(*) FROM dbo.AccountTransfer;
GO
