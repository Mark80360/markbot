---
name: skill-creator
description: >
  Create, edit, or package AgentSkills. Use when: (1) Creating a new skill from scratch,
  (2) Structuring or redesigning an existing skill, (3) Packaging a skill into a distributable
  .skill file, (4) Validating skill structure and metadata, (5) Improving skill descriptions
  or following best practices for skill design. Trigger when user asks to "create a skill",
  "make a skill", "package skill", "skill template", or needs help designing skill structure.
scripts:
  init_skill:
    entry: scripts/init_skill.py
    description: >
      Initialize a new skill directory with template SKILL.md and optional resources.
      Use to create a new skill from scratch.
    language: python
    parameters:
      skill_name:
        type: string
        description: Skill name (normalized to lowercase hyphen-case, e.g. "my-skill")
        required: true
      path:
        type: string
        description: Output directory path for the skill (e.g. skills/my-skill)
        required: true
      resources:
        type: string
        description: >
          Comma-separated list of resource types to create: scripts,references,assets
        required: false
      examples:
        type: boolean
        description: Create example placeholder files in resource directories
        required: false
  package_skill:
    entry: scripts/package_skill.py
    description: >
      Package a skill folder into a distributable .skill file (zip format).
      Validates skill structure before packaging.
    language: python
    parameters:
      skill_path:
        type: string
        description: Path to the skill folder to package
        required: true
      output_dir:
        type: string
        description: Optional output directory for the .skill file
        required: false
  validate_skill:
    entry: scripts/quick_validate.py
    description: >
      Validate skill structure, metadata, and naming conventions.
      Run before packaging to catch errors.
    language: python
    parameters:
      skill_path:
        type: string
        description: Path to the skill folder to validate
        required: true
  improve_description:
    entry: scripts/improve_description.py
    description: >
      Generate an improved SKILL.md description for better skill triggering.
    language: python
    parameters:
      skill_path:
        type: string
        description: Path to the skill folder containing SKILL.md
        required: true
---

# Skill Creator

This skill provides guidance and tools for creating effective skills.

## About Skills

Skills are modular packages that extend the agent's capabilities with specialized knowledge, workflows, and tools. Think of them as "onboarding guides" for specific domains.

### What Skills Provide

1. **Specialized workflows** - Multi-step procedures for specific domains
2. **Tool integrations** - Instructions for working with specific file formats or APIs
3. **Domain expertise** - Company-specific knowledge, schemas, business logic
4. **Bundled resources** - Scripts, references, and assets for complex and repetitive tasks

## Core Principles

### Concise is Key

Context window is shared with system prompt, conversation history, and other skills. Only add context the agent doesn't already have. Prefer concise examples over verbose explanations.

### Set Appropriate Degrees of Freedom

- **High freedom** (text instructions): Multiple valid approaches, context-dependent decisions
- **Medium freedom** (pseudocode/parameterized scripts): Preferred patterns exist
- **Low freedom** (specific scripts): Consistency critical, fragile operations

### Anatomy of a Skill

```
skill-name/
├── SKILL.md (required)
│   ├── YAML frontmatter (name, description)
│   └── Markdown instructions
└── Bundled Resources (optional)
    ├── scripts/     - Executable code (Python/Bash)
    ├── references/  - Documentation for context loading
    └── assets/      - Files used in output (templates, images)
```

### Progressive Disclosure

Skills use three-level loading:
1. **Metadata** (name + description) - Always in context (~100 words)
2. **SKILL.md body** - When skill triggers (<500 lines)
3. **Bundled resources** - As needed by agent

**Keep SKILL.md under 500 lines.** Split content into `references/` files when approaching this limit. Reference files should be one level deep from SKILL.md.

For detailed workflow patterns and output templates, see:
- **Workflow patterns**: Read `references/workflows.md` for sequential and conditional workflows
- **Output patterns**: Read `references/output-patterns.md` for templates and examples

## Skill Creation Process

Follow these steps in order:

### Step 1: Understand the Skill with Concrete Examples

Clarify concrete use cases before designing:
- "What functionality should this skill support?"
- "Can you give examples of how it would be used?"
- "What would a user say to trigger this skill?"

Skip if usage patterns are already clear.

### Step 2: Plan Reusable Contents

Analyze each concrete example to identify:
1. **scripts/** - Code rewritten repeatedly or needing deterministic reliability
2. **references/** - Documentation rediscovered each time (schemas, APIs, policies)
3. **assets/** - Boilerplate reused across outputs (templates, starter projects)

Example: A `pdf-editor` skill analyzing "Rotate this PDF" reveals:
1. Rotation code rewritten each time → `scripts/rotate_pdf.py`
2. Library docs needed → `references/pdf_operations.md`

### Step 3: Initialize the Skill

For new skills, always run `init_skill.py` to generate template structure:

```bash
scripts/init_skill.py <skill-name> --path <output-directory> [--resources scripts,references,assets] [--examples]
```

Examples:
```bash
scripts/init_skill.py my-skill --path skills
scripts/init_skill.py my-skill --path skills --resources scripts,references
scripts/init_skill.py my-skill --path skills --resources scripts --examples
```

The script:
- Creates skill directory
- Generates SKILL.md template with frontmatter and TODO placeholders
- Creates resource directories if `--resources` specified
- Adds example files if `--examples` set

Custom skills should live under workspace `skills/` directory (e.g., `<workspace>/skills/my-skill/SKILL.md`).

### Step 4: Edit the Skill

#### Start with Reusable Contents

Implement `scripts/`, `references/`, and `assets/` files first. Test scripts by running them to verify correctness.

If `--examples` was used, delete placeholder files not needed. Only create resource directories actually required.

#### Update SKILL.md

**Writing guidelines:** Use imperative/infinitive form.

**Frontmatter:**
- `name`: Skill name (lowercase, hyphens, digits only)
- `description`: Primary trigger - include what it does AND when to use it. All "when to use" info belongs here, not in body. Body is only loaded AFTER triggering.

Example description:
```yaml
description: "Comprehensive document creation, editing, and analysis with .docx files. Use when: (1) Creating new documents, (2) Modifying content, (3) Working with tracked changes, (4) Adding comments, (5) Extracting text"
```

**Body:**
- Instructions for using the skill and its resources
- Workflow decision trees for complex skills
- Code samples and concrete examples

Consult these guides based on skill needs:
- **Multi-step processes**: Read `references/workflows.md`
- **Specific output formats/quality**: Read `references/output-patterns.md`

### Step 5: Package the Skill

Once development is complete, validate and package into distributable `.skill` file:

```bash
scripts/package_skill.py <path/to/skill-folder> [output-directory]
```

The packaging script:
1. **Validates** skill automatically (frontmatter, naming, structure, description quality)
2. **Packages** into `.skill` file (zip format) if validation passes

Security: Symlinks are rejected. Fix validation errors before re-running.

### Step 6: Iterate

After real usage, improve based on experience:

1. Use the skill on actual tasks
2. Notice struggles or inefficiencies
3. Update SKILL.md or resources
4. Test again

Often happens right after using the skill with fresh context of how it performed.
