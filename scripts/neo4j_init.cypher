CREATE CONSTRAINT company_id IF NOT EXISTS FOR (c:Company) REQUIRE c.company_id IS UNIQUE;
CREATE CONSTRAINT industry_key IF NOT EXISTS FOR (i:Industry) REQUIRE (i.code, i.taxonomy) IS UNIQUE;
CREATE CONSTRAINT region_code IF NOT EXISTS FOR (r:Region) REQUIRE r.region_code IS UNIQUE;
CREATE CONSTRAINT contract_url IF NOT EXISTS FOR (c:Contract) REQUIRE c.notice_url IS UNIQUE;
CREATE INDEX notice_id IF NOT EXISTS FOR (n:TenderNotice) ON (n.notice_id);
