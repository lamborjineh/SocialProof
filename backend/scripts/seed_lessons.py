"""
SocialProof — Lesson Seed Data
scripts/seed_lessons.py

Seeds the 15-lesson curriculum defined in docx Section 7.
Each lesson covers one of 5 MIL topics at 3 difficulty levels.

Run with:
    python scripts/seed_lessons.py

Idempotent — uses INSERT IGNORE so existing rows are preserved.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sqlalchemy as sa
from database.models import engine

LESSONS = [
    # ── Claim Detection ───────────────────────────────────────────────────────
    {
        "lesson_key": "claim_detection_beginner",
        "title": "What Is a Factual Claim?",
        "topic": "claim_detection",
        "difficulty": "beginner",
        "mil_skill": "claim_identification",
        "sort_order": 1,
        "content": (
            "A factual claim is a statement that asserts something as true and can be "
            "verified or disproved with evidence. Not everything that sounds like a fact "
            "is a factual claim.\n\n"
            "Examples of factual claims:\n"
            "- 'The Philippines has a population of over 110 million.' (verifiable with census data)\n"
            "- 'WHO recommends washing hands for at least 20 seconds.' (verifiable with WHO guidelines)\n\n"
            "NOT factual claims:\n"
            "- 'This politician is terrible.' (opinion)\n"
            "- 'What do you think about the new law?' (question)\n"
            "- 'The game was exciting.' (subjective impression)\n\n"
            "When you encounter content online, your first job is to identify the factual claims — "
            "the specific assertions that could be true or false. Everything else (context, opinion, "
            "emotion, narrative) is secondary.\n\n"
            "Practice tip: Read a news article and underline every sentence that makes a specific, "
            "verifiable assertion. Those are your claims to check."
        ),
    },
    {
        "lesson_key": "claim_detection_intermediate",
        "title": "Claims vs. Opinions vs. Context",
        "topic": "claim_detection",
        "difficulty": "intermediate",
        "mil_skill": "claim_identification",
        "sort_order": 2,
        "content": (
            "Content rarely comes in pure form. Most articles, social posts, and videos mix "
            "factual claims with opinions, context, and emotional framing.\n\n"
            "The same underlying idea can appear as:\n"
            "- Claim: '43% of Filipino workers earn below the minimum wage.' (verifiable)\n"
            "- Opinion: 'Workers in the Philippines are exploited.' (judgment)\n"
            "- Context: 'The minimum wage has not been raised in three years.' (factual, but framing)\n\n"
            "Why this matters: Misinformation often works by embedding a false factual claim inside "
            "a paragraph of true context. The surrounding truth makes the false claim feel credible.\n\n"
            "Key question to ask: Can this specific sentence be verified independently with data "
            "from a named source, official record, or scientific study?\n\n"
            "If yes → it's a claim. If it requires interpretation or value judgment → it's an opinion.\n\n"
            "Advanced exercise: Take a social media post and classify every sentence as: "
            "Claim / Opinion / Context / Question / Emotional appeal."
        ),
    },
    {
        "lesson_key": "claim_detection_advanced",
        "title": "Hidden Claims and Implied Misinformation",
        "topic": "claim_detection",
        "difficulty": "advanced",
        "mil_skill": "claim_identification",
        "sort_order": 3,
        "content": (
            "The most dangerous misinformation doesn't lie outright — it implies. "
            "Content can convey a false impression without making a single technically false statement.\n\n"
            "Techniques used:\n"
            "1. Juxtaposition: 'Vaccination rates rose in 2022. So did cancer deaths.' "
            "   (implies causation without claiming it)\n"
            "2. Question framing: 'Should we be worried about what's in the vaccine?' "
            "   (implies danger without asserting it)\n"
            "3. Selective statistics: Citing a real number in a context where it misleads "
            "   (e.g., citing deaths from a rare side effect without citing base rates)\n"
            "4. Missing timeframe: 'Crime has increased' without specifying when or compared to what\n\n"
            "When checking content, ask: What is this piece of content trying to make me believe? "
            "Then check whether the evidence actually supports that belief — not just whether "
            "the individual sentences are technically accurate.\n\n"
            "This is the core of prebunking: understanding how you can be led to a false conclusion "
            "through entirely true statements presented selectively."
        ),
    },

    # ── Source Verification ───────────────────────────────────────────────────
    {
        "lesson_key": "source_verification_beginner",
        "title": "What Makes a Source Credible?",
        "topic": "source_verification",
        "difficulty": "beginner",
        "mil_skill": "source_check",
        "sort_order": 1,
        "content": (
            "Before trusting a claim, ask: who is making it and why?\n\n"
            "Basic credibility signals to check:\n\n"
            "Domain type:\n"
            "- .gov (government), .edu (university): generally authoritative for official data\n"
            "- .org: varies widely — check who runs it\n"
            "- .com: commercial interest may affect content\n"
            "- Unknown or new domains: highest skepticism\n\n"
            "HTTPS (padlock icon): means encrypted connection, NOT that the site is trustworthy. "
            "Fake sites use HTTPS too. Don't rely on this alone.\n\n"
            "Author attribution: Does the article name a specific author? Can you find that person?\n\n"
            "Publication date: Is this current? Old articles shared as new are a common trick.\n\n"
            "Quick credibility check order:\n"
            "1. Read the domain name carefully (misspelled official sites are common)\n"
            "2. Find the 'About Us' page — who funds the site?\n"
            "3. Search the author's name\n"
            "4. Check if other credible outlets are reporting the same story"
        ),
    },
    {
        "lesson_key": "source_verification_intermediate",
        "title": "Domain Signals and the About Page",
        "topic": "source_verification",
        "difficulty": "intermediate",
        "mil_skill": "source_check",
        "sort_order": 2,
        "content": (
            "Going deeper than the domain name: evaluating whether a website is a legitimate source.\n\n"
            "The About page is the most underused tool in source verification. It tells you:\n"
            "- Who owns the site (look for corporate or political backers)\n"
            "- What the site's stated mission is (advocacy vs. news vs. data reporting)\n"
            "- Who funds it (grants, ads, subscriptions, or undisclosed)\n\n"
            "Red flags on an About page:\n"
            "- No authors named\n"
            "- Claims to be 'independent' with no explanation of funding\n"
            "- Recent domain registration (check WHOIS)\n"
            "- No physical address or contact information\n\n"
            "Domain impersonation: Fake sites often use domains like 'ABCnews.com.co' or "
            "'reuters.net' to look like legitimate outlets. Always check the exact domain.\n\n"
            "Using Transparency tools:\n"
            "- WHOIS lookup: who registered the domain and when\n"
            "- NewsGuard / Media Bias/Fact Check: credibility ratings for news outlets\n"
            "- OpenCorporates: company registration data\n\n"
            "A credible source doesn't require you to trust it — it gives you the information "
            "to verify it independently."
        ),
    },
    {
        "lesson_key": "source_verification_advanced",
        "title": "Lateral Reading: Verify by Looking Away",
        "topic": "source_verification",
        "difficulty": "advanced",
        "mil_skill": "source_check",
        "sort_order": 3,
        "content": (
            "Professional fact-checkers and journalists don't verify a source by reading it more "
            "carefully — they verify it by opening multiple other tabs and searching for what "
            "others say about it. This is called lateral reading.\n\n"
            "The principle: The most efficient way to evaluate a source is to leave it immediately "
            "and look at it from the outside. A credible source will have an established "
            "reputation that others have documented.\n\n"
            "Lateral reading process:\n"
            "1. Copy the domain or outlet name\n"
            "2. Open a new tab and search: '[outlet name] bias' or '[outlet name] credibility'\n"
            "3. Check what fact-checkers, journalism schools, and watchdogs say about it\n"
            "4. Look for coverage of the specific story in other outlets\n\n"
            "Why vertical reading (reading the source itself more carefully) fails: "
            "A sophisticated misinformation site is designed to appear credible. "
            "It will cite real-looking sources, use professional design, and write in a neutral tone. "
            "You cannot detect this by reading it harder.\n\n"
            "Study finding: Stanford researchers found that professional fact-checkers spent "
            "seconds on a page before opening new tabs, while ordinary users spent minutes "
            "reading the original source. The fact-checkers were dramatically more accurate.\n\n"
            "Practice: Pick any unfamiliar website and lateral-read it in under 2 minutes."
        ),
    },

    # ── Bias Detection ────────────────────────────────────────────────────────
    {
        "lesson_key": "bias_detection_beginner",
        "title": "What Is Media Bias?",
        "topic": "bias_detection",
        "difficulty": "beginner",
        "mil_skill": "bias_check",
        "sort_order": 1,
        "content": (
            "Bias in media means a systematic slant or perspective that shapes how information "
            "is selected, framed, or presented. All media has some perspective — "
            "the question is whether that perspective distorts the truth.\n\n"
            "Types of bias:\n"
            "- Political bias: favouring one party, ideology, or policy position\n"
            "- Commercial bias: stories chosen for clicks or advertising revenue\n"
            "- Confirmation bias in reporting: only covering evidence that fits a pre-decided narrative\n"
            "- Cultural bias: assuming one culture's norms are universal\n\n"
            "Bias ≠ lying. A biased article can be factually accurate while still misleading "
            "by choosing which facts to include.\n\n"
            "The framing effect: Two headlines can describe the same event very differently:\n"
            "- 'Government distributes food aid to 10,000 families'\n"
            "- 'Government admits 10,000 families are starving'\n\n"
            "Both might be factually accurate. The framing determines whether you trust the government.\n\n"
            "Quick bias check: Who benefits if I believe this? What would the opposite framing look like?"
        ),
    },
    {
        "lesson_key": "bias_detection_intermediate",
        "title": "Language and Framing Signals",
        "topic": "bias_detection",
        "difficulty": "intermediate",
        "mil_skill": "bias_check",
        "sort_order": 2,
        "content": (
            "Biased content can usually be detected through specific language patterns. "
            "Learning to spot these patterns makes you more resistant to manipulation.\n\n"
            "Emotional language: Words designed to trigger a feeling rather than convey information.\n"
            "- 'Shocking revelation' vs. 'New study finds'\n"
            "- 'Desperate' vs. 'Facing challenges'\n"
            "- 'Radical agenda' vs. 'Policy proposal'\n\n"
            "Loaded language: Words that carry implied judgment.\n"
            "- 'Regime' (implies illegitimacy) vs. 'Government'\n"
            "- 'Admitted' (implies guilt) vs. 'Said'\n"
            "- 'Claims' (implies doubt) vs. 'States'\n\n"
            "Exaggeration and absolutes: 'Never', 'always', 'everyone knows', 'only', 'total disaster'.\n\n"
            "What's omitted: Bias often operates through what is NOT said. "
            "Ask: What information would change how I interpret this? Is it missing?\n\n"
            "Practice: Take any news article and replace all the loaded words with neutral equivalents. "
            "Does the story feel different? That difference is the bias."
        ),
    },
    {
        "lesson_key": "bias_detection_advanced",
        "title": "Structural Bias and Omission",
        "topic": "bias_detection",
        "difficulty": "advanced",
        "mil_skill": "bias_check",
        "sort_order": 3,
        "content": (
            "The most sophisticated bias doesn't appear in individual words — it operates at "
            "the structural level: which stories get covered, which sources get quoted, "
            "and which perspectives are treated as requiring proof.\n\n"
            "Story selection bias: An outlet that covers every crime committed by one group "
            "while ignoring equivalent crimes by another group shapes perception through "
            "volume, not through any individual false statement.\n\n"
            "Source selection bias: Who gets quoted as an expert? The choice of sources "
            "determines which perspectives are treated as authoritative.\n\n"
            "False balance: Giving equal coverage to a fringe position and a scientific consensus "
            "(e.g., 'Scientists and climate skeptics disagree') creates the impression of an "
            "ongoing debate where none exists.\n\n"
            "Normalisation bias: Repeating something enough times makes it feel normal and true, "
            "regardless of its accuracy.\n\n"
            "Critical question: Not 'Is this article accurate?' but 'What worldview does this "
            "outlet's overall coverage pattern produce in regular readers?'\n\n"
            "Tools for detecting structural bias:\n"
            "- Compare coverage of the same event across multiple outlets\n"
            "- Track which stories an outlet consistently ignores\n"
            "- Use allsides.com or similar tools to see the media ecosystem"
        ),
    },

    # ── Evidence Evaluation ───────────────────────────────────────────────────
    {
        "lesson_key": "evidence_evaluation_beginner",
        "title": "What Counts as Evidence?",
        "topic": "evidence_evaluation",
        "difficulty": "beginner",
        "mil_skill": "evidence_check",
        "sort_order": 1,
        "content": (
            "Not everything presented to support a claim is actually evidence. "
            "Learning to distinguish evidence from assertion is a foundational skill.\n\n"
            "Evidence hierarchy (strongest to weakest):\n"
            "1. Systematic reviews and meta-analyses (pooled results from many studies)\n"
            "2. Peer-reviewed research studies\n"
            "3. Official statistics from government or international bodies\n"
            "4. Expert consensus statements\n"
            "5. Single studies (can be replicated)\n"
            "6. Expert opinion (without systematic data)\n"
            "7. Anecdote ('I know someone who...')\n"
            "8. Assertion ('Everyone knows that...')\n\n"
            "A claim is not evidence for itself. 'It's true because I say it is' is an assertion.\n\n"
            "Common evidence substitutes to watch for:\n"
            "- Testimonials: real people, but not representative\n"
            "- Correlation without causation: two things happen together doesn't mean one causes the other\n"
            "- Vague attribution: 'Studies show...' — which studies? Where?\n\n"
            "Quick check: Can I find the original source of this evidence? "
            "If not, treat it as unverified."
        ),
    },
    {
        "lesson_key": "evidence_evaluation_intermediate",
        "title": "Supporting vs. Contradicting Evidence",
        "topic": "evidence_evaluation",
        "difficulty": "intermediate",
        "mil_skill": "evidence_check",
        "sort_order": 2,
        "content": (
            "Real-world claims are rarely supported by unanimous evidence. "
            "Learning to navigate conflicting sources is essential for accurate judgment.\n\n"
            "When evidence conflicts:\n"
            "1. Check the date: Older studies may be superseded by newer research\n"
            "2. Check the sample size: A study of 50 people vs. a study of 50,000 people carries different weight\n"
            "3. Check for replication: Has the finding been reproduced by independent researchers?\n"
            "4. Check the methodology: Observational study vs. randomised controlled trial\n"
            "5. Check for funding conflicts: Industry-funded studies have higher rates of favourable findings\n\n"
            "The 'cherry picking' problem: Misinformation often cites real studies selectively, "
            "choosing the one study out of fifty that supports a desired conclusion. "
            "Always ask: what does the bulk of the evidence say?\n\n"
            "What to do when evidence is genuinely mixed:\n"
            "- 'Uncertain' is a valid and honest conclusion\n"
            "- State the uncertainty clearly: 'Evidence is mixed on this topic'\n"
            "- Avoid the false choice between 'definitely true' and 'definitely false'\n\n"
            "Practice: Search for a health claim and find both supporting and contradicting studies. "
            "Which carries more weight and why?"
        ),
    },
    {
        "lesson_key": "evidence_evaluation_advanced",
        "title": "Evaluating Evidence Quality",
        "topic": "evidence_evaluation",
        "difficulty": "advanced",
        "mil_skill": "evidence_check",
        "sort_order": 3,
        "content": (
            "Not all evidence in the right category is equally strong. "
            "Advanced evidence evaluation means assessing three dimensions: relevance, recency, and source quality.\n\n"
            "Relevance: Does the evidence actually address the specific claim?\n"
            "- A study on adults may not support a claim about children\n"
            "- Data from another country may not apply to the Philippine context\n"
            "- Lab studies may not transfer to real-world conditions\n\n"
            "Recency:\n"
            "- Science moves fast; a 2010 nutrition study may be obsolete\n"
            "- But recency alone isn't quality — a poorly designed 2024 study is weaker than "
            "  a robust 2015 study that has been replicated\n\n"
            "Source quality:\n"
            "- Journal impact factor and peer review process\n"
            "- Preprint vs. peer-reviewed publication\n"
            "- Retracted studies (check Retraction Watch)\n"
            "- Gray literature (government reports, WHO guidance) — often high quality but not peer-reviewed\n\n"
            "Statistical literacy basics:\n"
            "- Relative risk vs. absolute risk: '50% increased risk' could mean going from 2% to 3%\n"
            "- Confidence intervals: wide CI means uncertain estimate\n"
            "- p-value misuse: statistical significance ≠ practical significance\n\n"
            "The expert consensus heuristic: When individual studies conflict, what do the major "
            "scientific bodies (WHO, CDC, national health authorities) say? This isn't an "
            "appeal to authority — it's recognising that consensus aggregates many studies."
        ),
    },

    # ── General MIL ───────────────────────────────────────────────────────────
    {
        "lesson_key": "general_mil_beginner",
        "title": "Why Media and Information Literacy Matters",
        "topic": "general",
        "difficulty": "beginner",
        "mil_skill": "general_mil",
        "sort_order": 1,
        "content": (
            "Every day, billions of pieces of content are created and shared online. "
            "Not all of it is accurate. Not all of it is honest. "
            "Media and Information Literacy (MIL) is the set of skills that help you "
            "navigate this environment without being misled.\n\n"
            "Why misinformation spreads:\n"
            "- Emotional content travels faster than accurate content\n"
            "- We are wired to trust people we know — and we share from people we trust\n"
            "- Misinformation often confirms what we already believe\n"
            "- There is a speed asymmetry: false stories spread before fact-checks arrive\n\n"
            "Your responsibility: When you share something, you become part of the information chain. "
            "A share is an implicit endorsement. Even well-meaning people spread misinformation "
            "when they don't stop to verify.\n\n"
            "The one-second pause: Before sharing anything, ask one question: "
            "'Do I actually know this is true?' If the answer is no — pause.\n\n"
            "MIL is not about being cynical or distrusting everything. "
            "It's about developing the habit of healthy skepticism — "
            "asking the right questions before accepting or amplifying information."
        ),
    },
    {
        "lesson_key": "general_mil_intermediate",
        "title": "The 5Ws of Media Evaluation",
        "topic": "general",
        "difficulty": "intermediate",
        "mil_skill": "general_mil",
        "sort_order": 2,
        "content": (
            "When evaluating any piece of content, five questions cut through most misinformation:\n\n"
            "1. WHO made this?\n"
            "   - What outlet, organisation, or person created this content?\n"
            "   - What is their track record? Their incentive?\n"
            "   - Are they accountable (named authors, editorial standards)?\n\n"
            "2. WHAT type of content is this?\n"
            "   - News report / opinion / satire / advertisement / propaganda?\n"
            "   - Is the format transparent about what it is?\n\n"
            "3. WHY was this created?\n"
            "   - To inform? To persuade? To entertain? To sell?\n"
            "   - Who benefits if you believe this?\n\n"
            "4. WHEN was this published?\n"
            "   - Is this current or an old story being recirculated?\n"
            "   - Is the framing still accurate given what has happened since?\n\n"
            "5. HOW was this distributed?\n"
            "   - Did it come through a trusted outlet or a forwarded message?\n"
            "   - Was it recommended by an algorithm, a friend, or a journalist?\n\n"
            "These five questions take under 60 seconds and catch the majority of misinformation "
            "before you engage with or share it."
        ),
    },
    {
        "lesson_key": "general_mil_advanced",
        "title": "Filter Bubbles, Algorithms, and Confirmation Bias",
        "topic": "general",
        "difficulty": "advanced",
        "mil_skill": "general_mil",
        "sort_order": 3,
        "content": (
            "The most persistent threat to accurate information processing isn't misinformation "
            "from external sources — it's the way your own mind and the platforms you use work "
            "together to create a distorted information environment.\n\n"
            "Confirmation bias: The tendency to seek, favour, and remember information that "
            "confirms what you already believe. This is a universal human trait, not a personal flaw.\n\n"
            "How algorithms amplify it:\n"
            "- Social media platforms optimise for engagement, not accuracy\n"
            "- Content that triggers strong emotional reactions gets more engagement\n"
            "- You are shown more of what you engage with — creating a feedback loop\n"
            "- Over time, your feed becomes a curated version of reality that matches "
            "  and reinforces your existing beliefs\n\n"
            "The filter bubble effect:\n"
            "Different people with different political or social beliefs can see completely "
            "different realities on the same platform, making it nearly impossible to have "
            "a shared factual baseline for public debate.\n\n"
            "What to do:\n"
            "1. Deliberately follow sources that challenge your existing views\n"
            "2. Pay attention to the topics and perspectives your feed never shows you\n"
            "3. When you strongly agree or strongly disagree with something, apply extra scrutiny\n"
            "4. Use incognito mode or different accounts to see how your feed differs\n\n"
            "Calibration: The goal of MIL is not to doubt everything — it is to hold your "
            "beliefs with a confidence level that matches the strength of the evidence. "
            "Strong evidence → strong confidence. Weak evidence → appropriate uncertainty."
        ),
    },
]

# ── Seed runner ───────────────────────────────────────────────────────────────

def ensure_columns():
    """Add missing columns to lessons table if they don't exist (idempotent)."""
    migrations = [
        ("mil_skill",               "ALTER TABLE lessons ADD COLUMN mil_skill VARCHAR(50) NULL"),
        ("sort_order",              "ALTER TABLE lessons ADD COLUMN sort_order INT NULL"),
        ("prerequisite_lesson_id",  "ALTER TABLE lessons ADD COLUMN prerequisite_lesson_id INT NULL"),
    ]
    with engine.connect() as conn:
        for col, sql in migrations:
            try:
                conn.execute(sa.text(sql))
                conn.commit()
                print(f"  ✓ Added column: {col}")
            except Exception:
                pass  # Column already exists — safe to ignore


def seed():
    with engine.connect() as conn:
        inserted = 0
        skipped  = 0
        for lesson in LESSONS:
            try:
                result = conn.execute(
                    sa.text("""
                        INSERT IGNORE INTO lessons
                            (lesson_key, title, content, topic, difficulty,
                             mil_skill, sort_order, created_at)
                        VALUES
                            (:lesson_key, :title, :content, :topic, :difficulty,
                             :mil_skill, :sort_order, NOW())
                    """),
                    {
                        "lesson_key": lesson["lesson_key"],
                        "title":      lesson["title"],
                        "content":    lesson["content"],
                        "topic":      lesson["topic"],
                        "difficulty": lesson["difficulty"],
                        "mil_skill":  lesson.get("mil_skill"),
                        "sort_order": lesson.get("sort_order"),
                    },
                )
                if result.rowcount > 0:
                    inserted += 1
                    print(f"  ✓ Inserted: {lesson['lesson_key']}")
                else:
                    skipped += 1
                    print(f"  - Skipped (exists): {lesson['lesson_key']}")
            except Exception as e:
                print(f"  ✗ Error inserting {lesson['lesson_key']}: {e}")
        conn.commit()
        print(f"\nDone — {inserted} inserted, {skipped} already existed.")


if __name__ == "__main__":
    print(f"Seeding {len(LESSONS)} lessons into the lessons table...\n")
    ensure_columns()
    seed()
